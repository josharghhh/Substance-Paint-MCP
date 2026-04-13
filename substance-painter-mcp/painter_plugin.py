"""
Substance Painter MCP Plugin
============================
Drop this file into Substance Painter's python/plugins/ folder.
Painter will discover it automatically on launch.

It starts a TCP socket server on localhost:9877 that listens for JSON commands
from the MCP server (server.py) and executes them using the Painter Python API.

Usage:
  Python > painter_plugin (to enable/disable from inside Painter)
"""

import ast
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
import substance_painter.ui as sp_ui
import substance_painter.project as sp_project
import substance_painter.export as sp_export
import substance_painter.textureset as sp_textureset
import substance_painter.resource as sp_resource
import substance_painter.logging as sp_logging
import substance_painter.layerstack as sp_layerstack
import substance_painter.colormanagement as sp_colormanagement
import substance_painter.display as sp_display

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtWidgets

PLUGIN_WIDGETS = []
HOST = "0.0.0.0"
PORT = 9877
REQUEST_EXECUTION_TIMEOUT = 300
MAX_REQUEST_SIZE = 1_048_576  # 1 MB
MAX_RESPONSE_SIZE = 10_485_760  # 10 MB
CLIENT_RECV_TIMEOUT = 30  # seconds
MAX_CONCURRENT_CONNECTIONS = 10
MAX_EXECUTE_CODE_LENGTH = 10_000
FILE_QUEUE_POLL_INTERVAL = 0.2
FILE_QUEUE_ROOT = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), "SubstancePainterMCP")
FILE_QUEUE_REQUESTS_DIR = os.path.join(FILE_QUEUE_ROOT, "requests")
FILE_QUEUE_RESPONSES_DIR = os.path.join(FILE_QUEUE_ROOT, "responses")

# Attribute names that are blocked regardless of which object they appear on.
# This covers both project lifecycle ops and dunder-based sandbox escapes.
_BLOCKED_ATTR_NAMES = frozenset({
    # Project lifecycle — use the dedicated MCP tools instead
    "create", "open", "close", "save", "save_as",
    "save_as_copy", "save_as_template", "reload_mesh",
    # OS-level methods reachable via injected module references
    "system", "popen", "remove", "unlink", "rmdir", "rename",
    "rmtree", "move",
})

# Top-level names that must not be called directly.
_BLOCKED_CALL_NAMES = frozenset({
    "eval", "exec", "compile", "globals", "locals", "vars", "dir",
    "getattr", "setattr", "delattr", "__import__",
})


class _ASTValidator(ast.NodeVisitor):
    """Walk the AST and reject any dangerous construct."""

    def __init__(self):
        self.error = None

    def _fail(self, msg):
        if self.error is None:
            self.error = msg

    def visit_Import(self, node):
        self._fail("import statements are not allowed")

    def visit_ImportFrom(self, node):
        self._fail("import statements are not allowed")

    def visit_Attribute(self, node):
        attr = node.attr
        # Block all dunder attributes — these expose __dict__, __builtins__,
        # __loader__, __subclasses__, etc. which defeat the exec sandbox.
        if attr.startswith("__") and attr.endswith("__"):
            self._fail(f"Access to dunder attribute '{attr}' is not allowed")
            return
        if attr in _BLOCKED_ATTR_NAMES:
            self._fail(f"Attribute '{attr}' is not allowed via execute_python — use the dedicated MCP tools")
            return
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALL_NAMES:
            self._fail(f"Call to '{node.func.id}' is not allowed")
            return
        self.generic_visit(node)

    def visit_Global(self, node):
        # Rebinding injected module globals (sp_project, etc.) is unnecessary
        # and could cause confusing state; block it outright.
        self._fail("'global' statements are not allowed")

    def visit_Nonlocal(self, node):
        self._fail("'nonlocal' statements are not allowed")


def _validate_code_ast(code):
    """
    Parse *code* and walk its AST to detect dangerous constructs.
    Returns an error string if blocked, or None if the code looks safe.
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return f"Syntax error in code: {exc}"
    validator = _ASTValidator()
    validator.visit(tree)
    return validator.error


SAFE_BUILTINS = {
    "True": True,
    "False": False,
    "None": None,
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "frozenset": frozenset,
    "hasattr": hasattr,
    "int": int,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "print": print,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    # Exceptions — needed for try/except in user scripts
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "RuntimeError": RuntimeError,
    "AttributeError": AttributeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "NameError": NameError,
    "StopIteration": StopIteration,
    "NotImplementedError": NotImplementedError,
}
VALID_BAKE_MAP_TYPES = {"normal", "ao", "curvature", "position", "thickness", "id"}
VALID_RESOLUTIONS = {64, 128, 256, 512, 1024, 2048, 4096, 8192}


class HandlerDispatcher(QtCore.QObject):
    request = QtCore.Signal(object, object, object)

    def __init__(self):
        super().__init__()
        self.request.connect(self._dispatch, QtCore.Qt.QueuedConnection)

    @QtCore.Slot(object, object, object)
    def _dispatch(self, handler, params, state):
        def execute_handler():
            try:
                state["result"] = handler(params)
            except Exception as exc:
                state["result"] = {
                    "error": f"Handler '{handler.__name__}' failed: {exc}",
                    "traceback": traceback.format_exc(),
                }
            finally:
                state["event"].set()

        try:
            dispatcher = getattr(sp_project, "execute_when_not_busy", None)
            if dispatcher is not None:
                dispatcher(execute_handler)
            else:
                execute_handler()
        except Exception as exc:
            state["result"] = {
                "error": f"Failed to dispatch '{handler.__name__}': {exc}",
                "traceback": traceback.format_exc(),
            }
            state["event"].set()


HANDLER_DISPATCHER = None


def log_info(message):
    sp_logging.log(sp_logging.INFO, "PainterMCP", message)


def log_warning(message):
    sp_logging.log(sp_logging.WARNING, "PainterMCP", message)


def log_error(message):
    sp_logging.log(sp_logging.ERROR, "PainterMCP", message)


def get_handler_dispatcher():
    global HANDLER_DISPATCHER

    if HANDLER_DISPATCHER is None:
        HANDLER_DISPATCHER = HandlerDispatcher()

        app = QtCore.QCoreApplication.instance()
        if app is not None:
            HANDLER_DISPATCHER.moveToThread(app.thread())

    return HANDLER_DISPATCHER


def resolution_to_list(resolution):
    if hasattr(resolution, "width") and hasattr(resolution, "height"):
        return [int(resolution.width), int(resolution.height)]

    if isinstance(resolution, (list, tuple)) and len(resolution) == 2:
        return [int(resolution[0]), int(resolution[1])]

    raise ValueError(f"Unsupported resolution value: {resolution!r}")


def normalize_native_path(path_value):
    """Convert WSL-style paths to Windows paths for the Painter process."""
    path_str = str(path_value or "").strip()
    if not path_str:
        return ""

    wsl_match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", path_str)
    if wsl_match:
        drive = wsl_match.group(1).upper()
        suffix = wsl_match.group(2).replace("/", "\\")
        return f"{drive}:\\{suffix}"

    return os.path.realpath(os.path.abspath(path_str))


def import_js_module():
    try:
        import substance_painter.js as sp_js
    except ImportError:
        return None
    return sp_js


def remove_widget(widget):
    delete_ui_element = getattr(sp_ui, "delete_ui_element", None)
    if callable(delete_ui_element):
        delete_ui_element(widget)
        return

    widget.setParent(None)
    widget.deleteLater()


def run_handler_on_painter_thread(handler, params):
    completed = threading.Event()
    state = {"event": completed}
    dispatcher = get_handler_dispatcher()

    if QtCore.QThread.currentThread() == dispatcher.thread():
        dispatcher._dispatch(handler, params, state)
    else:
        dispatcher.request.emit(handler, params, state)

    if not completed.wait(REQUEST_EXECUTION_TIMEOUT):
        return {"error": f"Handler '{handler.__name__}' timed out in Painter."}

    return state.get("result", {"error": f"Handler '{handler.__name__}' returned no result."})


# ---------------------------------------------------------------------------
# Command handlers
# Each handler receives a dict of params and returns a JSON-serialisable dict.
# ---------------------------------------------------------------------------

def handle_get_project_info(params):
    if not sp_project.is_open():
        return {"error": "No project is open in Substance Painter."}

    url = sp_project.file_path()

    texture_sets = []
    for ts in sp_textureset.all_texture_sets():
        stacks = []
        for stack in ts.all_stacks():
            channels = [str(ch) for ch in stack.all_channels()]
            stacks.append({"name": str(stack), "channels": channels})
        texture_sets.append({
            "name": ts.name(),
            "resolution": resolution_to_list(ts.get_resolution()),
            "stacks": stacks,
        })

    return {
        "project_file": url,
        "texture_sets": texture_sets,
        "texture_set_count": len(texture_sets),
    }


def handle_list_texture_sets(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    sets = []
    for ts in sp_textureset.all_texture_sets():
        sets.append({
            "name": ts.name(),
            "resolution": resolution_to_list(ts.get_resolution()),
        })
    return {"texture_sets": sets}


def resolve_export_preset(preset_name):
    if not preset_name:
        return None

    if "://" in preset_name:
        if not preset_name.startswith("resource://"):
            return None
        return preset_name

    normalized_name = str(preset_name).strip().lower()

    for preset in sp_export.list_predefined_export_presets():
        if str(getattr(preset, "name", "")).strip().lower() == normalized_name:
            return getattr(preset, "url", None)

    for preset in sp_export.list_resource_export_presets():
        resource_id = getattr(preset, "resource_id", None)
        if resource_id is None:
            continue

        if str(getattr(resource_id, "name", "")).strip().lower() == normalized_name:
            url_getter = getattr(resource_id, "url", None)
            if callable(url_getter):
                return url_getter()

    return None


def handle_export_textures(params):
    """
    Export textures using a named export preset.
    params:
      preset_name  - name of the export preset (e.g. "PBR Metallic Roughness")
      output_path  - directory where textures will be written
      texture_sets - optional list of texture set names to export (all if omitted)
    """
    if not sp_project.is_open():
        return {"error": "No project is open."}

    preset_name = params.get("preset_name", "PBR Metallic Roughness")
    output_path = params.get("output_path", "")
    ts_filter = params.get("texture_sets", None)

    # Validate output_path — normalise and optionally restrict to an allowed root.
    # Set the SP_MCP_EXPORT_ROOT environment variable to restrict exports to a
    # specific directory tree (e.g. "C:\Users\me\Textures"). When unset, any
    # existing directory accessible to the Painter process is accepted.
    if not output_path:
        return {"error": "output_path is required."}
    output_path = os.path.realpath(os.path.abspath(output_path))
    if not os.path.isdir(output_path):
        return {"error": f"Invalid output_path: '{output_path}' — directory does not exist."}
    allowed_root = os.environ.get("SP_MCP_EXPORT_ROOT", "").strip()
    if allowed_root:
        allowed_root = os.path.realpath(os.path.abspath(allowed_root))
        try:
            if os.path.commonpath([output_path, allowed_root]) != allowed_root:
                return {"error": f"output_path '{output_path}' is outside the allowed export root '{allowed_root}'."}
        except ValueError:
            # commonpath raises ValueError on Windows when paths are on different drives
            return {"error": f"output_path '{output_path}' is outside the allowed export root '{allowed_root}'."}

    preset_identifier = resolve_export_preset(preset_name)

    if not preset_identifier:
        return {"error": f"Unknown export preset: '{preset_name}'"}

    # Validate texture_sets filter
    if ts_filter is not None:
        if not isinstance(ts_filter, list):
            return {"error": "texture_sets must be a list of texture set names."}
        if len(ts_filter) == 0:
            return {"error": "texture_sets is empty — omit the parameter to export all sets, or provide specific names."}
        available_names = [ts.name() for ts in sp_textureset.all_texture_sets()]
        unknown_names = [name for name in ts_filter if name not in available_names]
        if unknown_names:
            return {
                "error": f"Unknown texture set(s): {unknown_names}. Available: {available_names}",
            }

    # Build export config
    export_config = {
        "exportShaderParams": False,
        "exportPath": output_path,
        "defaultExportPreset": preset_identifier,
        "exportList": [],
        "exportParameters": [{
            "parameters": {
                "paddingAlgorithm": "infinite",
                "dithering": True,
            }
        }],
    }

    for ts in sp_textureset.all_texture_sets():
        if ts_filter and ts.name() not in ts_filter:
            continue
        export_config["exportList"].append({
            "rootPath": ts.name(),
        })

    if not export_config["exportList"]:
        return {"error": "No texture sets to export — exportList is empty."}

    try:
        result = sp_export.export_project_textures(export_config)
        exported = []
        for status in result.textures.values():
            exported.extend(status)
        return {
            "success": True,
            "exported_files": exported,
            "count": len(exported),
        }
    except Exception as e:
        return {"error": str(e)}


def handle_bake_maps(params):
    """
    Trigger baking via the legacy JavaScript API (the Python API doesn't expose
    baking directly yet, so we go through the JS bridge).
    params:
      texture_set  - name of the texture set to bake
      maps         - list of map types, e.g. ["normal", "ao", "curvature"]
      resolution   - [width, height], e.g. [2048, 2048]
    """
    if not sp_project.is_open():
        return {"error": "No project is open."}

    sp_js = import_js_module()
    if sp_js is None:
        return {"error": "This version of Substance Painter does not expose the JavaScript bridge needed for baking."}

    ts_name = params.get("texture_set")
    if not ts_name:
        return {"error": "'texture_set' is required."}

    valid_ts_names = {ts.name() for ts in sp_textureset.all_texture_sets()}
    if ts_name not in valid_ts_names:
        return {"error": f"Texture set '{ts_name}' not found. Available: {sorted(valid_ts_names)}"}

    maps = params.get("maps", ["normal", "ao", "curvature", "position", "thickness"])
    if not isinstance(maps, list) or not maps:
        maps = ["normal", "ao", "curvature"]

    invalid_maps = [m for m in maps if m.lower() not in VALID_BAKE_MAP_TYPES]
    if invalid_maps:
        return {"error": f"Invalid map types: {invalid_maps}. Valid types: {sorted(VALID_BAKE_MAP_TYPES)}"}

    resolution = params.get("resolution", [2048, 2048])
    if not isinstance(resolution, list) or len(resolution) != 2:
        resolution = [2048, 2048]
    try:
        resolution = [int(resolution[0]), int(resolution[1])]
    except (TypeError, ValueError):
        resolution = [2048, 2048]

    if resolution[0] not in VALID_RESOLUTIONS or resolution[1] not in VALID_RESOLUTIONS:
        return {"error": f"Resolution values must be powers of 2 between 64 and 8192. Got: {resolution}"}

    ts_name_json = json.dumps(ts_name)
    maps_json = json.dumps([str(m).lower() for m in maps])
    resolution_json = json.dumps(resolution)

    # Try modern API first, then fall back to legacy API for older Painter versions.
    # Also try passing a map filter directly to bake when supported.
    js_code = f"""
    (function() {{
        var textureSetName = {ts_name_json};
        var requestedMaps = {maps_json};
        var bakeResolution = {resolution_json};
        var apiMode = "unknown";

        function applyResolution(node) {{
            if (!node || typeof node !== "object") {{
                return false;
            }}

            var changed = false;

            if (Array.isArray(node)) {{
                for (var i = 0; i < node.length; i++) {{
                    changed = applyResolution(node[i]) || changed;
                }}
                return changed;
            }}

            if (Object.prototype.hasOwnProperty.call(node, "Resolution")) {{
                node.Resolution = bakeResolution;
                changed = true;
            }}

            var keys = Object.keys(node);
            for (var j = 0; j < keys.length; j++) {{
                changed = applyResolution(node[keys[j]]) || changed;
            }}

            return changed;
        }}

        function runBakeWithOptionalMapFilter() {{
            try {{
                alg.baking.bake(textureSetName, requestedMaps);
                return true;
            }} catch (mapFilterErr) {{
                alg.baking.bake(textureSetName);
                return false;
            }}
        }}

        try {{
            var tsParams = alg.baking.textureSetBakingParameters(textureSetName);
            applyResolution(tsParams);
            if (typeof alg.baking.setTextureSetBakingParameters === "function") {{
                alg.baking.setTextureSetBakingParameters(textureSetName, tsParams);
            }}

            var mapFilterAppliedNew = runBakeWithOptionalMapFilter();
            apiMode = mapFilterAppliedNew ? "texture_set_params_with_map_filter" : "texture_set_params_no_map_filter";
        }} catch (newApiErr) {{
            var commonParams = alg.baking.commonBakingParameters();
            if (commonParams && commonParams.commonParameters && Object.prototype.hasOwnProperty.call(commonParams.commonParameters, "Resolution")) {{
                commonParams.commonParameters.Resolution = bakeResolution;
            }} else {{
                applyResolution(commonParams);
            }}

            if (typeof alg.baking.setCommonBakingParameters === "function") {{
                alg.baking.setCommonBakingParameters(commonParams);
            }}

            var mapFilterAppliedLegacy = runBakeWithOptionalMapFilter();
            apiMode = mapFilterAppliedLegacy ? "legacy_common_with_map_filter" : "legacy_common_no_map_filter";
        }}

        return {{
            success: true,
            texture_set: textureSetName,
            requested_maps: requestedMaps,
            resolution: bakeResolution,
            api_mode: apiMode
        }};
    }})()
    """
    try:
        result = sp_js.evaluate(js_code)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as e:
        return {"error": str(e)}


def handle_get_export_presets(params):
    """List available export presets with Python API first and JS fallbacks."""
    try:
        presets = []

        for preset in sp_export.list_predefined_export_presets():
            name = getattr(preset, "name", None)
            if name:
                presets.append(str(name))

        for preset in sp_export.list_resource_export_presets():
            resource_id = getattr(preset, "resource_id", None)
            name = getattr(resource_id, "name", None)
            if name:
                presets.append(str(name))

        if presets:
            return {"presets": sorted(set(presets), key=str.lower)}

        sp_js = import_js_module()
        if sp_js is None:
            return {"presets": []}

        js_presets = sp_js.evaluate(
            """
            (function() {
                var found = [];
                try {
                    found = alg.resources.findResources("export-preset") || [];
                } catch (e) {
                    found = [];
                }

                var unique = [];
                var seen = {};
                for (var i = 0; i < found.length; i++) {
                    var item = found[i];
                    var value = item && (item.label || item.name || String(item));
                    if (!seen[value]) {
                        seen[value] = true;
                        unique.push(value);
                    }
                }
                return unique;
            })()
            """
        )
        if isinstance(js_presets, list):
            return {"presets": js_presets}
        return {"presets": []}
    except Exception as e:
        return {"error": str(e)}


def handle_execute_code(params):
    """
    Run arbitrary Python code inside Painter. Powerful but use carefully.
    params:
      code - Python source string to exec
    """
    code = params.get("code", "")
    if not code or not code.strip():
        return {"error": "No code provided."}
    if len(code) > MAX_EXECUTE_CODE_LENGTH:
        return {"error": f"Code too long ({len(code)} chars). Maximum is {MAX_EXECUTE_CODE_LENGTH}."}
    ast_error = _validate_code_ast(code)
    if ast_error:
        return {"error": ast_error}

    local_vars = {}
    try:
        exec(code, {  # noqa: S102
            "__builtins__": SAFE_BUILTINS,
            "sp_project": sp_project,
            "sp_export": sp_export,
            "sp_textureset": sp_textureset,
            "sp_resource": sp_resource,
            "sp_logging": sp_logging,
            "sp_layerstack": sp_layerstack,
            "sp_colormanagement": sp_colormanagement,
            "sp_display": sp_display,
            "sp_js": import_js_module(),  # may be None on some SP versions
        }, local_vars)
        return {"success": True, "locals": {k: str(v) for k, v in local_vars.items()}}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def handle_save_project(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    try:
        save_mode = getattr(sp_project, "ProjectSaveMode", None)
        if save_mode is not None and hasattr(save_mode, "Full"):
            sp_project.save(save_mode.Full)
        else:
            sp_project.save()
        return {"success": True, "message": "Project saved."}
    except Exception as e:
        return {"error": str(e)}


def handle_reload_mesh(params):
    """Reload the current project mesh and wait for Painter's async callback."""
    if not sp_project.is_open():
        return {"error": "No project is open."}

    mesh_file_path = normalize_native_path(params.get("mesh_file_path", ""))
    if not mesh_file_path:
        return {"error": "mesh_file_path is required."}
    if not os.path.isfile(mesh_file_path):
        return {"error": f"Invalid mesh_file_path: '{mesh_file_path}' — file does not exist."}

    settings_cls = getattr(sp_project, "MeshReloadingSettings", None)
    status_enum = getattr(sp_project, "ReloadMeshStatus", None)
    if settings_cls is None or status_enum is None:
        return {"error": "This version of Substance Painter does not expose mesh reloading APIs."}

    settings = settings_cls(
        import_cameras=bool(params.get("import_cameras", True)),
        preserve_strokes=bool(params.get("preserve_strokes", True)),
    )

    completed = threading.Event()
    status_info = {
        "raw": None,
        "name": "UNKNOWN",
        "value": None,
        "success": None,
    }

    success_value = int(getattr(status_enum, "SUCCESS", 0))
    error_value = int(getattr(status_enum, "ERROR", 2))

    def loading_status_cb(status):
        try:
            status_value = getattr(status, "value", None)
            if status_value is None:
                try:
                    status_value = int(status)
                except Exception:
                    status_value = None

            status_name = getattr(status, "name", None)
            if status_name is None and status_value is not None:
                if status_value == success_value:
                    status_name = "SUCCESS"
                elif status_value == error_value:
                    status_name = "ERROR"
            if status_name is None:
                status_name = str(status)

            status_info["raw"] = str(status)
            status_info["name"] = status_name
            status_info["value"] = status_value
            if status_value is not None:
                status_info["success"] = status_value == success_value
            else:
                status_info["success"] = bool(status == getattr(status_enum, "SUCCESS", None))
        finally:
            completed.set()

    try:
        sp_project.reload_mesh(mesh_file_path, settings, loading_status_cb)
    except Exception as e:
        return {"error": str(e)}

    if not completed.wait(REQUEST_EXECUTION_TIMEOUT):
        return {
            "success": True,
            "status": "PENDING",
            "message": "Mesh reload started; Painter did not report completion before the timeout.",
            "mesh_file_path": mesh_file_path,
        }

    if status_info["success"]:
        return {
            "success": True,
            "status": status_info["name"],
            "status_value": status_info["value"],
            "mesh_file_path": mesh_file_path,
            "import_cameras": bool(params.get("import_cameras", True)),
            "preserve_strokes": bool(params.get("preserve_strokes", True)),
        }

    return {
        "error": (
            f"Mesh reload failed with status '{status_info['name']}'. "
            "Check the Painter log for details."
        ),
        "status": status_info["name"],
        "status_value": status_info["value"],
        "mesh_file_path": mesh_file_path,
    }


# ---------------------------------------------------------------------------
# Channel-type helpers
# ---------------------------------------------------------------------------

_CHANNEL_TYPE_MAP = {
    "basecolor":         "BaseColor",
    "roughness":         "Roughness",
    "metallic":          "Metallic",
    "normal":            "Normal",
    "height":            "Height",
    "opacity":           "Opacity",
    "emissive":          "Emissive",
    "specularlevel":     "SpecularLevel",
    "anisotropylevel":   "AnisotropyLevel",
    "anisotropyangle":   "AnisotropyAngle",
    "transmissive":      "Transmissive",
    "absorption":        "Absorption",
    "absorptioncolor":   "Absorption",
    "scattering":        "Scattering",
    "scatteringcolor":   "Scattering",
    "coatopacity":       "CoatOpacity",
    "coatroughness":     "CoatRoughness",
    "coatnormal":        "CoatNormal",
    "coatcolor":         "CoatColor",
    "coatspecularlevel": "CoatSpecularLevel",
    "sheenopacity":      "SheenOpacity",
    "sheenroughness":    "SheenRoughness",
    "sheencolor":        "SheenColor",
    "blendingmask":      "BlendingMask",
    "ior":               "Ior",
    "diffuse":           "Diffuse",
    "specular":          "Specular",
    "glossiness":        "Glossiness",
    "ambientocclusion":  "AmbientOcclusion",
    "ao":                "AmbientOcclusion",
    "reflection":        "Reflection",
    "displacement":      "Displacement",
    "user0": "User0", "user1": "User1", "user2": "User2", "user3": "User3",
    "user4": "User4", "user5": "User5", "user6": "User6", "user7": "User7",
}

# Fallback names for ChannelType enum members that vary across Painter versions.
# Each canonical name maps to a list of alternatives to try via getattr.
_CHANNEL_ENUM_FALLBACKS = {
    "AmbientOcclusion": ["AmbientOcclusion", "AO"],
    "SpecularLevel":    ["SpecularLevel", "Specularlevel"],
    "AnisotropyLevel":  ["AnisotropyLevel", "Anisotropylevel"],
    "AnisotropyAngle":  ["AnisotropyAngle", "Anisotropyangle"],
    "Absorption":       ["Absorption", "AbsorptionColor"],
    "Scattering":       ["Scattering", "ScatteringColor"],
    "CoatColor":        ["CoatColor"],
    "Ior":              ["Ior", "IOR"],
}


def _resolve_channel_type(channel_type_str):
    key = channel_type_str.replace(" ", "").replace("_", "").lower()
    canonical = _CHANNEL_TYPE_MAP.get(key)
    if canonical is None:
        valid = sorted(set(_CHANNEL_TYPE_MAP.values()), key=str.lower)
        return None, f"Unknown channel type '{channel_type_str}'. Valid types: {valid}"
    # Try the canonical name first, then version-specific fallbacks
    candidates = _CHANNEL_ENUM_FALLBACKS.get(canonical, [canonical])
    for name in candidates:
        ct = getattr(sp_textureset.ChannelType, name, None)
        if ct is not None:
            return ct, None
    return None, f"Channel type '{canonical}' is not supported by this version of Substance Painter."


def _get_texture_set(ts_name):
    for ts in sp_textureset.all_texture_sets():
        if ts.name() == ts_name:
            return ts, None
    available = [ts.name() for ts in sp_textureset.all_texture_sets()]
    return None, f"Texture set '{ts_name}' not found. Available: {available}"


# ---------------------------------------------------------------------------
# New handlers — project status, resolution, channels
# ---------------------------------------------------------------------------

def handle_get_project_status(params):
    try:
        is_open = bool(sp_project.is_open())
        if is_open:
            try:
                is_saved = bool(sp_project.is_saved())
            except Exception:
                is_saved = False
            try:
                needs_saving = bool(sp_project.needs_saving())
            except Exception:
                needs_saving = not is_saved
            try:
                fp = sp_project.file_path()
                file_path = str(fp) if fp else None
            except Exception:
                file_path = None
        else:
            is_saved = False
            needs_saving = False
            file_path = None
        return {"is_open": is_open, "is_saved": is_saved,
                "needs_saving": needs_saving, "file_path": file_path}
    except Exception as e:
        return {"error": f"Failed to get project status: {e}"}


def handle_set_texture_set_resolution(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    if not ts_name or not isinstance(ts_name, str) or not ts_name.strip():
        return {"error": "'texture_set' is required."}
    try:
        width = int(params["width"])
    except (KeyError, TypeError, ValueError):
        return {"error": "'width' is required and must be an integer."}
    try:
        height = int(params["height"])
    except (KeyError, TypeError, ValueError):
        return {"error": "'height' is required and must be an integer."}
    if width not in VALID_RESOLUTIONS:
        return {"error": f"Invalid width {width}. Must be one of: {sorted(VALID_RESOLUTIONS)}"}
    if height not in VALID_RESOLUTIONS:
        return {"error": f"Invalid height {height}. Must be one of: {sorted(VALID_RESOLUTIONS)}"}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    try:
        old = resolution_to_list(ts.get_resolution())
        ts.set_resolution(sp_textureset.Resolution(width, height))
        new = resolution_to_list(ts.get_resolution())
        return {"success": True, "texture_set": ts_name,
                "old_resolution": old, "new_resolution": new}
    except Exception as e:
        return {"error": f"Failed to set resolution: {e}"}


def handle_list_channels(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name_filter = params.get("texture_set")
    if ts_name_filter is not None:
        ts_obj, err = _get_texture_set(ts_name_filter)
        if err:
            return {"error": err}
        all_ts = [ts_obj]
    else:
        all_ts = list(sp_textureset.all_texture_sets())
    result_sets = []
    for ts in all_ts:
        stacks_data = []
        for stack in ts.all_stacks():
            try:
                channels = stack.all_channels()
                if isinstance(channels, dict):
                    channel_names = [str(ct.name) for ct in channels.keys()]
                else:
                    channel_names = [str(ch) for ch in channels]
            except Exception:
                channel_names = []
            stacks_data.append({"name": str(stack), "channels": channel_names})
        result_sets.append({"name": ts.name(), "stacks": stacks_data})
    return {"texture_sets": result_sets}


def handle_add_channel(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    if not ts_name or not isinstance(ts_name, str):
        return {"error": "'texture_set' is required."}
    channel_type_str = params.get("channel_type")
    if not channel_type_str or not isinstance(channel_type_str, str):
        return {"error": "'channel_type' is required."}
    ct, err = _resolve_channel_type(channel_type_str)
    if err:
        return {"error": err}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    try:
        stacks = list(ts.all_stacks())
        if not stacks:
            return {"error": f"Texture set '{ts_name}' has no stacks."}
        stack = stacks[0]
        existing = stack.all_channels()
        existing_types = set(existing.keys()) if isinstance(existing, dict) else set()
        if ct in existing_types:
            return {"error": f"Channel '{channel_type_str}' already exists in '{ts_name}'."}
        # Determine a sensible default channel format.
        # Color channels use sRGB8; data channels use L16F; normal uses RGB16.
        _COLOR_CHANNELS = {"BaseColor", "Diffuse", "Emissive", "SheenColor",
                           "CoatColor", "Specular", "Reflection", "AO",
                           "AmbientOcclusion", "AbsorptionColor", "ScatteringColor"}
        _NORMAL_CHANNELS = {"Normal", "CoatNormal"}
        ct_name = ct.name if hasattr(ct, "name") else str(ct)
        if ct_name in _NORMAL_CHANNELS:
            fmt = sp_textureset.ChannelFormat.RGB16
        elif ct_name in _COLOR_CHANNELS:
            fmt = sp_textureset.ChannelFormat.sRGB8
        else:
            fmt = sp_textureset.ChannelFormat.L16F
        stack.add_channel(ct, fmt)
        updated = stack.all_channels()
        channel_names = ([str(k.name) for k in updated.keys()]
                         if isinstance(updated, dict) else [str(ch) for ch in updated])
        return {"success": True, "texture_set": ts_name, "channel_added": channel_type_str,
                "channels": channel_names}
    except Exception as e:
        return {"error": f"Failed to add channel: {e}"}


def handle_remove_channel(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    if not ts_name or not isinstance(ts_name, str):
        return {"error": "'texture_set' is required."}
    channel_type_str = params.get("channel_type")
    if not channel_type_str or not isinstance(channel_type_str, str):
        return {"error": "'channel_type' is required."}
    ct, err = _resolve_channel_type(channel_type_str)
    if err:
        return {"error": err}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    try:
        stacks = list(ts.all_stacks())
        if not stacks:
            return {"error": f"Texture set '{ts_name}' has no stacks."}
        stack = stacks[0]
        existing = stack.all_channels()
        existing_types = set(existing.keys()) if isinstance(existing, dict) else set()
        if ct not in existing_types:
            present = ([str(k.name) for k in existing_types] if existing_types
                       else [str(ch) for ch in stack.all_channels()])
            return {"error": f"Channel '{channel_type_str}' not found in '{ts_name}'. Present: {present}"}
        stack.remove_channel(ct)
        updated = stack.all_channels()
        channel_names = ([str(k.name) for k in updated.keys()]
                         if isinstance(updated, dict) else [str(ch) for ch in updated])
        return {"success": True, "texture_set": ts_name, "channel_removed": channel_type_str,
                "channels": channel_names}
    except Exception as e:
        return {"error": f"Failed to remove channel: {e}"}


# ---------------------------------------------------------------------------
# Layer-stack helpers
# ---------------------------------------------------------------------------

def _get_root_nodes(stack):
    """Return root layer nodes for a stack, trying both known API shapes."""
    fn = getattr(sp_layerstack, "get_root_layer_nodes", None)
    if callable(fn):
        try:
            return list(fn(stack))
        except Exception:
            pass
    try:
        return list(stack.get_nodes())
    except Exception:
        return []


def _find_node_by_name(nodes, name):
    """Recursively find the first node matching *name* (depth-first)."""
    for node in nodes:
        try:
            if node.get_name() == name:
                return node
        except Exception:
            pass
        children = _get_root_nodes(node) if hasattr(node, "get_nodes") else []
        found = _find_node_by_name(children, name)
        if found is not None:
            return found
    return None


def _serialize_node(node):
    """Convert a Node to a plain JSON-serialisable dict."""
    try:
        name = node.get_name()
    except Exception:
        name = "<unknown>"
    try:
        nt = node.get_type()
        if nt == sp_layerstack.NodeType.FillLayer:
            node_type = "FillLayer"
        elif nt == sp_layerstack.NodeType.PaintLayer:
            node_type = "PaintLayer"
        elif nt == sp_layerstack.NodeType.GroupLayer:
            node_type = "GroupLayer"
        else:
            node_type = str(nt)
    except Exception:
        node_type = "unknown"
    try:
        enabled = node.is_visible()
    except Exception:
        enabled = None
    try:
        has_mask = node.has_mask()
    except Exception:
        has_mask = None
    children = []
    try:
        child_nodes = _get_root_nodes(node)
        children = [_serialize_node(c) for c in child_nodes]
    except Exception:
        pass
    return {"name": name, "type": node_type, "enabled": enabled,
            "has_mask": has_mask, "children": children}


def _make_insert_position(stack, above_layer_name=None):
    """
    Return an InsertPosition for the given stack.
    If *above_layer_name* is given, insert above that layer; otherwise insert
    at the top of the Content stack.

    NOTE: NodeStack values are Substack / Content / Mask — NOT Normal.
    InsertPosition.inside_node() requires a layer node (with .uid()), not a
    Stack object. Passing a Stack raises AttributeError: 'Stack' has no
    attribute 'uid'. Always get root nodes and use above_node() when possible.
    """
    InsertPosition = sp_layerstack.InsertPosition

    if above_layer_name:
        root_nodes = _get_root_nodes(stack)
        target = _find_node_by_name(root_nodes, above_layer_name)
        if target is None:
            return None, f"Layer '{above_layer_name}' not found."
        return InsertPosition.above_node(target), None

    # Fall back: insert above the current top node in the stack.
    # inside_node(Stack, ...) fails — Stack has no .uid(). Instead, get the
    # topmost existing node and insert above it. If the stack is empty, we
    # cannot reliably position without a node reference; callers should handle
    # this case separately.
    root_nodes = _get_root_nodes(stack)
    if root_nodes:
        return InsertPosition.above_node(root_nodes[0]), None

    # Empty stack: construct InsertPosition directly using (stack_id, InsertPositionLocation).
    # InsertPosition(int, InsertPositionLocation) with stack.stack_id inserts at the top of the
    # content stack. NodeStack.Content.value gives the InsertPositionLocation enum needed by the
    # C binding (NodeStack wraps InsertPositionLocation; passing NodeStack itself causes a type error).
    try:
        loc = sp_layerstack.NodeStack.Content.value
        return InsertPosition(stack.stack_id, loc), None
    except Exception as e:
        return None, f"Cannot determine insert position for empty stack: {e}"


def _get_mask_nodes(layer_node):
    """
    Return the list of effect/fill nodes inside a layer's mask.
    Uses InsertPosition to read the mask content stack.
    Returns (nodes_list, error_string). nodes_list may be empty if mask has no contents.
    """
    if not layer_node.has_mask():
        return [], f"Layer '{layer_node.get_name()}' has no mask."
    try:
        # To enumerate mask contents we insert a probe and immediately look at
        # the node's children via a dedicated mask root accessor if available.
        get_fn = getattr(sp_layerstack, "get_mask_layer_nodes", None)
        if callable(get_fn):
            return list(get_fn(layer_node)), None
        # Fallback: SP doesn't always expose get_mask_layer_nodes.
        # We can reconstruct by checking for FillEffectNode / GeneratorEffectNode
        # children — but without a direct accessor this is best-effort.
        return [], None
    except Exception as e:
        return [], str(e)


# ---------------------------------------------------------------------------
# Layer-stack handlers
# ---------------------------------------------------------------------------

def handle_list_layers(params):
    """List the full layer tree for a texture set."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    if ts_name:
        ts, err = _get_texture_set(ts_name)
        if err:
            return {"error": err}
    else:
        all_ts = list(sp_textureset.all_texture_sets())
        if not all_ts:
            return {"error": "No texture sets in project."}
        ts = all_ts[0]
    stacks_data = []
    for stack in ts.all_stacks():
        nodes = _get_root_nodes(stack)
        stacks_data.append({"stack": str(stack),
                             "layers": [_serialize_node(n) for n in nodes]})
    return {"texture_set": ts.name(), "stacks": stacks_data}


def _insert_layer_node(params, insert_fn_name, default_name, type_label):
    """Shared implementation for add_fill_layer / add_paint_layer / add_group_layer."""
    try:
        if not sp_project.is_open():
            return {"error": "No project is open."}
        ts_name = params.get("texture_set")
        if not ts_name:
            return {"error": "'texture_set' is required."}
        ts, err = _get_texture_set(ts_name)
        if err:
            return {"error": err}
        stacks = list(ts.all_stacks())
        if not stacks:
            return {"error": f"Texture set '{ts_name}' has no stacks."}
        stack = stacks[0]
        layer_name = str(params.get("name", default_name)).strip() or default_name
        above_layer = params.get("above_layer")
        pos, err = _make_insert_position(stack, above_layer)
        if err:
            return {"error": err}
        insert_fn = getattr(sp_layerstack, insert_fn_name, None)
        if insert_fn is None:
            return {"error": f"sp_layerstack.{insert_fn_name} is not available in this version of Substance Painter."}
        with sp_layerstack.ScopedModification(f"Add {type_label}"):
            new_node = insert_fn(pos)
            new_node.set_name(layer_name)
        return {"success": True, "texture_set": ts_name,
                "layer_name": new_node.get_name(), "layer_type": type_label}
    except Exception as e:
        return {"error": f"Failed to add {type_label}: {e}"}


def handle_add_fill_layer(params):
    return _insert_layer_node(params, "insert_fill", "Fill Layer", "FillLayer")


def handle_add_paint_layer(params):
    return _insert_layer_node(params, "insert_paint", "Paint Layer", "PaintLayer")


def handle_add_group_layer(params):
    return _insert_layer_node(params, "insert_group", "Group", "GroupLayer")


def handle_rename_layer(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    new_name = params.get("new_name", "").strip()
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    if not new_name:
        return {"error": "'new_name' is required."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    nodes = _get_root_nodes(ts.all_stacks()[0])
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}
    try:
        with sp_layerstack.ScopedModification("Rename Layer"):
            node.set_name(new_name)
        return {"success": True, "texture_set": ts_name,
                "old_name": layer_name, "new_name": new_name}
    except Exception as e:
        return {"error": f"Failed to rename layer: {e}"}


def handle_set_layer_enabled(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    enabled = params.get("enabled")
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    if not isinstance(enabled, bool):
        return {"error": "'enabled' must be a boolean."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    nodes = _get_root_nodes(ts.all_stacks()[0])
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}
    try:
        with sp_layerstack.ScopedModification("Set Layer Enabled"):
            node.set_visible(enabled)
        return {"success": True, "texture_set": ts_name,
                "layer_name": layer_name, "enabled": enabled}
    except Exception as e:
        return {"error": f"Failed to set layer enabled: {e}"}


def handle_delete_layer(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    nodes = _get_root_nodes(ts.all_stacks()[0])
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}
    delete_fn = getattr(sp_layerstack, "delete_node", None) or getattr(sp_layerstack, "remove_node", None)
    if delete_fn is None:
        return {"error": "Neither sp_layerstack.delete_node nor remove_node is available in this version of Substance Painter."}
    try:
        with sp_layerstack.ScopedModification("Delete Layer"):
            delete_fn(node)
        return {"success": True, "texture_set": ts_name, "deleted_layer": layer_name}
    except Exception as e:
        return {"error": f"Failed to delete layer: {e}"}


def handle_add_layer_mask(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    background = str(params.get("background", "White")).strip().capitalize()
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    if background not in ("White", "Black"):
        return {"error": "'background' must be 'White' or 'Black'."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    nodes = _get_root_nodes(ts.all_stacks()[0])
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}
    try:
        if node.has_mask():
            return {"error": f"Layer '{layer_name}' already has a mask. Remove it first."}
        bg = getattr(sp_layerstack.MaskBackground, background)
        with sp_layerstack.ScopedModification("Add Layer Mask"):
            node.add_mask(bg)
        return {"success": True, "texture_set": ts_name,
                "layer_name": layer_name, "background": background}
    except Exception as e:
        return {"error": f"Failed to add mask: {e}"}


def handle_remove_layer_mask(params):
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    nodes = _get_root_nodes(ts.all_stacks()[0])
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}
    try:
        if not node.has_mask():
            return {"error": f"Layer '{layer_name}' has no mask to remove."}
        with sp_layerstack.ScopedModification("Remove Layer Mask"):
            node.remove_mask()
        return {"success": True, "texture_set": ts_name, "layer_name": layer_name}
    except Exception as e:
        return {"error": f"Failed to remove mask: {e}"}


def handle_set_fill_layer_color(params):
    """Set the fill color of a named fill layer's channel."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set", "")
    layer_name = params.get("layer_name", "").strip()
    channel = str(params.get("channel", "basecolor")).strip().lower()
    color = params.get("color")
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    if not isinstance(color, (list, tuple)) or len(color) != 3:
        return {"error": "'color' must be a list of three floats [r, g, b] in the 0-1 range."}
    try:
        r, g, b = float(color[0]), float(color[1]), float(color[2])
    except (TypeError, ValueError):
        return {"error": "'color' values must be numeric floats."}
    for comp, val in (("r", r), ("g", g), ("b", b)):
        if not (0.0 <= val <= 1.0):
            return {"error": f"Color component '{comp}' ({val}) must be in the 0-1 range."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}

    # Map channel name to ChannelType enum
    _CHANNEL_MAP = {
        "basecolor": "BaseColor", "base_color": "BaseColor", "diffuse": "BaseColor",
        "height": "Height", "emissive": "Emissive", "emission": "Emissive",
        "roughness": "Roughness", "metallic": "Metallic", "normal": "Normal",
        "opacity": "Opacity", "glossiness": "Glossiness",
        "specularlevel": "SpecularLevel", "specular_level": "SpecularLevel",
        "ambientocclusion": "AmbientOcclusion", "ao": "AmbientOcclusion",
    }
    canonical_ch = _CHANNEL_MAP.get(channel, channel.title())
    ch_type = getattr(sp_textureset.ChannelType, canonical_ch, None)
    if ch_type is None:
        return {"error": f"Unknown channel '{channel}'."}

    nodes = _get_root_nodes(ts.all_stacks()[0])
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}

    try:
        new_color = sp_colormanagement.Color(r, g, b)
        with sp_layerstack.ScopedModification("Set Fill Color"):
            src = node.get_source(ch_type)
            if src is not None and hasattr(src, "set_color"):
                src.set_color(new_color)
            else:
                node.set_source(ch_type, new_color)
        return {"success": True, "texture_set": ts_name, "layer_name": layer_name,
                "channel": canonical_ch, "color": [r, g, b]}
    except Exception as e:
        return {"error": f"Failed to set fill color: {e}"}


def _match_resource_type(res, resource_type):
    """Check if a Resource object matches the requested MCP resource type.
    Uses Resource.type() (sp_resource.Type enum) and Resource.category().
    """
    # Mapping from MCP type to (Type enum name, category constraint).
    # category=None means "any", a string means exact match, callable is a predicate.
    _TYPE_FILTER = {
        "material":       ("SUBSTANCE", lambda c: c != "" and c != "Filter"),
        "texture":        ("IMAGE", None),
        "brush":          ("BRUSH", None),
        "filter":         ("SUBSTANCE", "Filter"),
        "smart-material": ("SMART_MATERIAL", None),
        "smart-mask":     ("SMART_MASK", None),
        "export-preset":  ("EXPORT", None),
        # alpha & generator are both SUBSTANCE with empty category; best-effort
        "alpha":          ("SUBSTANCE", ""),
        "generator":      ("SUBSTANCE", ""),
    }
    spec = _TYPE_FILTER.get(resource_type)
    if spec is None:
        return True
    type_name, cat_rule = spec
    type_enum = getattr(sp_resource.Type, type_name, None)
    if type_enum is None:
        return True
    if res.type() != type_enum:
        return False
    if cat_rule is None:
        return True
    cat = res.category()
    if callable(cat_rule):
        return cat_rule(cat)
    return cat == cat_rule


def handle_search_resources(params):
    """Search Substance Painter resources (materials, alphas, textures, etc.)."""
    query = str(params.get("query", "")).strip()
    if not query:
        return {"error": "'query' is required."}
    resource_type = str(params.get("type", "")).strip().lower() if params.get("type") else None
    _TYPE_ALIASES = {
        "material": "material", "alpha": "alpha", "texture": "texture",
        "brush": "brush", "filter": "filter", "generator": "generator",
        "smart-material": "smart-material", "smart_material": "smart-material",
        "smartmaterial": "smart-material", "smart-mask": "smart-mask",
        "smart_mask": "smart-mask", "smartmask": "smart-mask",
        "export-preset": "export-preset", "export_preset": "export-preset",
    }
    if resource_type:
        resource_type = _TYPE_ALIASES.get(resource_type, resource_type)
    try:
        search_fn = getattr(sp_resource, "search", None)
        if callable(search_fn):
            raw = list(search_fn(query))
            query_lower = query.lower()
            matches = []
            for res in raw:
                rid = res.identifier()
                gui = res.gui_name()
                rname = rid.name
                # Client-side name filter (search API may return all resources)
                if query_lower not in gui.lower() and query_lower not in rname.lower():
                    continue
                # Type filter
                if resource_type:
                    try:
                        if not _match_resource_type(res, resource_type):
                            continue
                    except Exception:
                        pass
                try:
                    url = rid.url()
                except Exception:
                    url = str(rid)
                matches.append({"name": gui or rname, "url": url})
            return {"query": query, "type": resource_type, "count": len(matches),
                    "resources": matches}
    except Exception:
        pass
    sp_js = import_js_module()
    if sp_js is None:
        return {"error": "sp_resource.search() unavailable and no JavaScript bridge.", "query": query}
    query_json = json.dumps(query)
    type_json = json.dumps(resource_type) if resource_type else "null"
    js_code = f"""
    (function() {{
        var query = {query_json}, resourceType = {type_json}, ql = query.toLowerCase();
        try {{
            var all = alg.resources.findResources(query) || [];
            var matches = [];
            for (var i = 0; i < all.length; i++) {{
                var item = all[i], label = item && (item.label || item.name || String(item));
                if (label && label.toLowerCase().indexOf(ql) !== -1)
                    matches.push({{name: label, url: item && item.url ? String(item.url) : String(item)}});
            }}
            return {{query: query, type: resourceType, count: matches.length, resources: matches}};
        }} catch(e) {{ return {{error: "JS resource search failed: " + String(e), query: query}}; }}
    }})()
    """
    try:
        result = sp_js.evaluate(js_code)
        return result if isinstance(result, dict) else {"query": query, "result": result}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Display & camera handlers
# ---------------------------------------------------------------------------

def handle_get_camera(params):
    """Get the default camera properties."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    try:
        camera = sp_display.Camera.get_default_camera()
        proj_type = camera.projection_type
        proj_name = proj_type.name if hasattr(proj_type, "name") else str(proj_type)
        return {
            "position": list(camera.position),
            "rotation": list(camera.rotation),
            "field_of_view": float(camera.field_of_view),
            "focal_length": float(camera.focal_length),
            "focus_distance": float(camera.focus_distance),
            "aperture": float(camera.aperture),
            "projection_type": proj_name,
            "orthographic_height": float(camera.orthographic_height),
        }
    except Exception as e:
        return {"error": f"Failed to get camera: {e}"}


def handle_set_camera(params):
    """Set camera properties. Only provided fields are updated."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    try:
        camera = sp_display.Camera.get_default_camera()
        changed = []

        if "position" in params:
            pos = params["position"]
            if not isinstance(pos, list) or len(pos) != 3:
                return {"error": "'position' must be a list of 3 floats [x, y, z]."}
            camera.position = [float(pos[0]), float(pos[1]), float(pos[2])]
            changed.append("position")

        if "rotation" in params:
            rot = params["rotation"]
            if not isinstance(rot, list) or len(rot) != 3:
                return {"error": "'rotation' must be a list of 3 floats [x, y, z] in degrees."}
            camera.rotation = [float(rot[0]), float(rot[1]), float(rot[2])]
            changed.append("rotation")

        if "field_of_view" in params:
            fov = float(params["field_of_view"])
            camera.field_of_view = fov
            changed.append("field_of_view")

        if "focal_length" in params:
            fl = float(params["focal_length"])
            camera.focal_length = fl
            changed.append("focal_length")

        if "focus_distance" in params:
            fd = float(params["focus_distance"])
            camera.focus_distance = fd
            changed.append("focus_distance")

        if "aperture" in params:
            ap = float(params["aperture"])
            camera.aperture = ap
            changed.append("aperture")

        if "projection_type" in params:
            pt_str = str(params["projection_type"]).strip().lower()
            pt_map = {"perspective": "Perspective", "orthographic": "Orthographic"}
            canonical = pt_map.get(pt_str)
            if canonical is None:
                return {"error": f"Invalid projection_type '{params['projection_type']}'. Use 'Perspective' or 'Orthographic'."}
            camera.projection_type = getattr(sp_display.CameraProjectionType, canonical)
            changed.append("projection_type")

        if "orthographic_height" in params:
            oh = float(params["orthographic_height"])
            camera.orthographic_height = oh
            changed.append("orthographic_height")

        if not changed:
            return {"error": "No camera properties provided to update."}

        return {"success": True, "updated": changed}
    except Exception as e:
        return {"error": f"Failed to set camera: {e}"}


def handle_get_environment_map(params):
    """Get the current environment map resource."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    try:
        res = sp_display.get_environment_resource()
        if res is None:
            return {"environment_map": None}
        return {"environment_map": res.url()}
    except Exception as e:
        return {"error": f"Failed to get environment map: {e}"}


def handle_set_environment_map(params):
    """Set the environment map by resource URL."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    url = params.get("url", "").strip()
    if not url:
        return {"error": "'url' is required (resource URL of the environment map)."}
    try:
        res_id = sp_resource.ResourceID.from_url(url)
        sp_display.set_environment_resource(res_id)
        return {"success": True, "environment_map": url}
    except Exception as e:
        return {"error": f"Failed to set environment map: {e}"}


def handle_get_tone_mapping(params):
    """Get the current tone mapping function."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    try:
        tm = sp_display.get_tone_mapping()
        name = tm.name if hasattr(tm, "name") else str(tm)
        return {"tone_mapping": name}
    except RuntimeError as e:
        return {"error": f"Tone mapping unavailable (project may be color managed): {e}"}
    except Exception as e:
        return {"error": f"Failed to get tone mapping: {e}"}


def handle_set_tone_mapping(params):
    """Set the tone mapping function (Linear or ACES)."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    value = str(params.get("tone_mapping", "")).strip()
    if not value:
        return {"error": "'tone_mapping' is required ('Linear' or 'ACES')."}
    tm_map = {"linear": "Linear", "aces": "ACES"}
    canonical = tm_map.get(value.lower())
    if canonical is None:
        return {"error": f"Invalid tone_mapping '{value}'. Use 'Linear' or 'ACES'."}
    try:
        tm = getattr(sp_display.ToneMappingFunction, canonical)
        sp_display.set_tone_mapping(tm)
        return {"success": True, "tone_mapping": canonical}
    except RuntimeError as e:
        return {"error": f"Tone mapping unavailable (project may be color managed): {e}"}
    except Exception as e:
        return {"error": f"Failed to set tone mapping: {e}"}


def handle_get_color_lut(params):
    """Get the current color profile LUT resource."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    try:
        res = sp_display.get_color_lut_resource()
        if res is None:
            return {"color_lut": None}
        return {"color_lut": res.url()}
    except Exception as e:
        return {"error": f"Failed to get color LUT: {e}"}


def handle_set_color_lut(params):
    """Set the color profile LUT by resource URL. Pass empty string to clear."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    url = params.get("url", "").strip()
    if not url:
        return {"error": "'url' is required (resource URL of the color LUT)."}
    try:
        res_id = sp_resource.ResourceID.from_url(url)
        sp_display.set_color_lut_resource(res_id)
        return {"success": True, "color_lut": url}
    except Exception as e:
        return {"error": f"Failed to set color LUT: {e}"}


def handle_set_display_channel(params):
    """Channel isolation is not supported by the Substance Painter API.

    Neither alg.display (JS) nor sp_display (Python) exposes per-channel view mode
    control. alg.display only has: getEnvironmentResource, setEnvironmentResource,
    getColorLutResource, setColorLutResource. The channel solo toggle is purely a
    UI-only feature with no programmatic access.
    """
    return {
        "error": (
            "set_display_channel is not supported: Substance Painter provides no API "
            "(Python or JavaScript) to control the 3D viewport channel display mode. "
            "Use the Solo Channel buttons in the Painter UI directly."
        )
    }


# ---------------------------------------------------------------------------
# Layer enhancement handlers
# ---------------------------------------------------------------------------

def handle_set_layer_opacity(params):
    """Set the opacity of a layer (0.0 to 1.0)."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    opacity = params.get("opacity")
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    if opacity is None:
        return {"error": "'opacity' is required (float 0.0 to 1.0)."}
    try:
        opacity = float(opacity)
    except (TypeError, ValueError):
        return {"error": "'opacity' must be a number."}
    if not (0.0 <= opacity <= 1.0):
        return {"error": f"'opacity' must be between 0.0 and 1.0, got {opacity}."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    stack = ts.all_stacks()[0]
    nodes = _get_root_nodes(stack)
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}
    try:
        with sp_layerstack.ScopedModification("Set Layer Opacity"):
            try:
                node.set_opacity(opacity)
            except Exception:
                # Main-stack nodes require a ChannelType argument
                for ch in stack.all_channels():
                    node.set_opacity(opacity, ch)
        return {"success": True, "texture_set": ts_name,
                "layer_name": layer_name, "opacity": opacity}
    except Exception as e:
        return {"error": f"Failed to set opacity: {e}"}


def handle_set_layer_blending_mode(params):
    """Set the blending mode of a layer."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    mode_str = params.get("blending_mode", "").strip()
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    if not mode_str:
        return {"error": "'blending_mode' is required."}
    # Build a map of lowercase names to BlendingMode enum values
    blending_modes = {}
    for attr in dir(sp_layerstack.BlendingMode):
        if not attr.startswith("_"):
            blending_modes[attr.lower()] = attr
    canonical = blending_modes.get(mode_str.lower())
    if canonical is None:
        available = sorted(blending_modes.values())
        return {"error": f"Unknown blending mode '{mode_str}'. Available: {available}"}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    stack = ts.all_stacks()[0]
    nodes = _get_root_nodes(stack)
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}
    try:
        bm = getattr(sp_layerstack.BlendingMode, canonical)
        with sp_layerstack.ScopedModification("Set Blending Mode"):
            try:
                node.set_blending_mode(bm)
            except Exception:
                # Main-stack nodes require a ChannelType argument
                for ch in stack.all_channels():
                    node.set_blending_mode(bm, ch)
        return {"success": True, "texture_set": ts_name,
                "layer_name": layer_name, "blending_mode": canonical}
    except Exception as e:
        return {"error": f"Failed to set blending mode: {e}"}


def handle_duplicate_layer(params):
    """Duplicate a layer in a texture set."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    nodes = _get_root_nodes(ts.all_stacks()[0])
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}
    dup_fn = getattr(sp_layerstack, "duplicate_node", None) or getattr(sp_layerstack, "clone_node", None)
    if dup_fn is not None:
        try:
            with sp_layerstack.ScopedModification("Duplicate Layer"):
                new_node = dup_fn(node)
            new_name = new_node.get_name() if hasattr(new_node, "get_name") else str(new_node)
            return {"success": True, "texture_set": ts_name,
                    "original": layer_name, "duplicate": new_name}
        except Exception as e:
            return {"error": f"Failed to duplicate layer: {e}"}
    # Fallback: use the JavaScript bridge (alg.layers API)
    sp_js_mod = import_js_module()
    if sp_js_mod is None:
        return {"error": "sp_layerstack.duplicate_node is not available and JS bridge is unavailable."}
    js_code = """
    (function() {
        var tsName = TS_NAME;
        var layerName = LAYER_NAME;
        try {
            var nodes = alg.layers.getNodes(tsName);
            function findNode(list, name) {
                for (var i = 0; i < list.length; i++) {
                    if (alg.layers.getName(list[i]) === name) return list[i];
                    try {
                        var ch = alg.layers.getNodes(list[i]);
                        if (ch && ch.length) { var f = findNode(ch, name); if (f) return f; }
                    } catch(e) {}
                }
                return null;
            }
            var node = findNode(nodes, layerName);
            if (!node) return {error: "Layer '" + layerName + "' not found via JS."};
            var newNode = alg.layers.duplicateNode(node);
            var newName = alg.layers.getName(newNode);
            return {success: true, texture_set: tsName, original: layerName, duplicate: newName};
        } catch(e) { return {error: "JS duplicate failed: " + String(e)}; }
    })()
    """.replace("TS_NAME", json.dumps(ts_name)).replace("LAYER_NAME", json.dumps(layer_name))
    try:
        result = sp_js_mod.evaluate(js_code)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as e:
        return {"error": f"Failed to duplicate layer via JS: {e}"}


def handle_move_layer(params):
    """Move a layer above or below another layer, or to the top/bottom."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    target_layer = params.get("target_layer", "").strip()
    position = str(params.get("position", "above")).strip().lower()
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    if position not in ("above", "below"):
        return {"error": "'position' must be 'above' or 'below'."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    stacks = list(ts.all_stacks())
    if not stacks:
        return {"error": f"Texture set '{ts_name}' has no stacks."}
    stack = stacks[0]
    nodes = _get_root_nodes(stack)
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}
    move_fn = getattr(sp_layerstack, "move_node", None)
    InsertPosition = sp_layerstack.InsertPosition
    if target_layer:
        target = _find_node_by_name(nodes, target_layer)
        if target is None:
            return {"error": f"Target layer '{target_layer}' not found in '{ts_name}'."}
        if position == "above":
            insert_pos = InsertPosition.above_node(target)
        else:
            insert_pos = InsertPosition.below_node(target)
    else:
        # Move to top of stack — use above the first root node.
        if nodes:
            insert_pos = InsertPosition.above_node(nodes[0])
        else:
            return {"error": "Stack is empty, cannot determine insert position."}

    if move_fn is not None:
        try:
            with sp_layerstack.ScopedModification("Move Layer"):
                move_fn(node, insert_pos)
            return {"success": True, "texture_set": ts_name,
                    "layer_name": layer_name, "position": position,
                    "target_layer": target_layer or "(top of stack)"}
        except Exception as e:
            return {"error": f"Failed to move layer: {e}"}

    # Fallback: use the JavaScript bridge (alg.layers API)
    sp_js_mod = import_js_module()
    if sp_js_mod is None:
        return {"error": "sp_layerstack.move_node is not available and JS bridge is unavailable."}

    target_name_js = json.dumps(target_layer) if target_layer else "null"
    js_code = """
    (function() {
        var tsName = TS_NAME;
        var layerName = LAYER_NAME;
        var targetName = TARGET_NAME;
        var position = POSITION;
        try {
            var nodes = alg.layers.getNodes(tsName);
            function findNode(list, name) {
                for (var i = 0; i < list.length; i++) {
                    if (alg.layers.getName(list[i]) === name) return list[i];
                    try {
                        var ch = alg.layers.getNodes(list[i]);
                        if (ch && ch.length) { var f = findNode(ch, name); if (f) return f; }
                    } catch(e) {}
                }
                return null;
            }
            var node = findNode(nodes, layerName);
            if (!node) return {error: "Layer '" + layerName + "' not found via JS."};
            if (targetName) {
                var target = findNode(nodes, targetName);
                if (!target) return {error: "Target '" + targetName + "' not found via JS."};
                if (position === "above") {
                    alg.layers.moveNode(node, target, "above");
                } else {
                    alg.layers.moveNode(node, target, "below");
                }
            } else {
                alg.layers.moveNode(node, nodes[0], "above");
            }
            return {success: true, texture_set: tsName, layer_name: layerName,
                    position: position, target_layer: targetName || "(top of stack)"};
        } catch(e) { return {error: "JS move failed: " + String(e)}; }
    })()
    """.replace("TS_NAME", json.dumps(ts_name)) \
       .replace("LAYER_NAME", json.dumps(layer_name)) \
       .replace("TARGET_NAME", target_name_js) \
       .replace("POSITION", json.dumps(position))
    try:
        result = sp_js_mod.evaluate(js_code)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as e:
        return {"error": f"Failed to move layer via JS: {e}"}


# ---------------------------------------------------------------------------
# Project & scene handlers
# ---------------------------------------------------------------------------

def handle_get_scene_bounding_box(params):
    """Get the scene bounding box (dimensions, center, radius)."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    try:
        bb = sp_project.get_scene_bounding_box()
        return {
            "dimensions": list(bb.dimensions),
            "center": list(bb.center),
            "radius": float(bb.radius),
        }
    except Exception as e:
        return {"error": f"Failed to get bounding box: {e}"}


def handle_get_project_metadata(params):
    """Get a metadata value from the project."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    key = str(params.get("key", "")).strip()
    context = str(params.get("context", "project")).strip()
    if not key:
        return {"error": "'key' is required."}
    try:
        metadata = sp_project.Metadata(context)
        value = metadata.get(key)
        return {"key": key, "context": context, "value": value}
    except Exception as e:
        return {"error": f"Failed to get metadata '{key}': {e}"}


def handle_set_project_metadata(params):
    """Set a metadata value on the project."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    key = str(params.get("key", "")).strip()
    value = params.get("value")
    context = str(params.get("context", "project")).strip()
    if not key:
        return {"error": "'key' is required."}
    if value is None:
        return {"error": "'value' is required."}
    try:
        metadata = sp_project.Metadata(context)
        metadata.set(key, value)
        return {"success": True, "key": key, "context": context, "value": value}
    except Exception as e:
        return {"error": f"Failed to set metadata '{key}': {e}"}


def handle_list_project_metadata(params):
    """List all metadata keys in the project."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    context = str(params.get("context", "project")).strip()
    try:
        metadata = sp_project.Metadata(context)
        keys = metadata.list()
        return {"context": context, "keys": keys}
    except Exception as e:
        return {"error": f"Failed to list metadata: {e}"}


# ---------------------------------------------------------------------------
# Texture set extra handlers
# ---------------------------------------------------------------------------

def handle_get_uv_tiles(params):
    """Get UV tiles for a texture set with per-tile resolution."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    if not ts_name:
        return {"error": "'texture_set' is required."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    try:
        if not ts.has_uv_tiles():
            return {"texture_set": ts_name, "has_uv_tiles": False, "tiles": []}
        tiles = []
        for tile in ts.all_uv_tiles():
            tile_info = {"u": tile.u, "v": tile.v}
            try:
                tile_info["name"] = tile.name
            except Exception:
                tile_info["name"] = None
            try:
                tile_info["resolution"] = resolution_to_list(tile.get_resolution())
            except Exception:
                tile_info["resolution"] = None
            try:
                tile_info["mesh_names"] = tile.all_mesh_names()
            except Exception:
                tile_info["mesh_names"] = []
            tiles.append(tile_info)
        return {"texture_set": ts_name, "has_uv_tiles": True, "tiles": tiles}
    except Exception as e:
        return {"error": f"Failed to get UV tiles: {e}"}


def handle_get_mesh_names(params):
    """Get mesh names assigned to a texture set."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    if not ts_name:
        return {"error": "'texture_set' is required."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    try:
        names = ts.all_mesh_names()
        return {"texture_set": ts_name, "mesh_names": names}
    except Exception as e:
        return {"error": f"Failed to get mesh names: {e}"}


# ---------------------------------------------------------------------------
# Resource management handlers
# ---------------------------------------------------------------------------

def handle_list_shelves(params):
    """List all resource shelves."""
    try:
        shelves = sp_resource.Shelves.all()
        result = []
        for shelf in shelves:
            result.append({
                "name": shelf.name(),
                "path": shelf.path(),
                "can_import": shelf.can_import_resources(),
                "is_crawling": shelf.is_crawling(),
            })
        return {"shelves": result}
    except Exception as e:
        return {"error": f"Failed to list shelves: {e}"}


def handle_import_resource(params):
    """Import a resource file into the project."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    file_path = str(params.get("file_path", "")).strip()
    usage = str(params.get("usage", "")).strip().lower()
    name = params.get("name")
    group = params.get("group")
    if not file_path:
        return {"error": "'file_path' is required."}
    if not os.path.isfile(file_path):
        return {"error": f"File not found: '{file_path}'."}
    if not usage:
        return {"error": "'usage' is required (e.g. 'texture', 'alpha', 'environment', 'base_material', 'smart_material', 'smart_mask', 'generator', 'filter', 'brush')."}
    usage_map = {}
    for attr in dir(sp_resource.Usage):
        if not attr.startswith("_"):
            usage_map[attr.lower()] = attr
    canonical = usage_map.get(usage.replace("-", "_").replace(" ", "_"))
    if canonical is None:
        available = sorted(usage_map.values())
        return {"error": f"Unknown usage '{usage}'. Available: {available}"}
    try:
        usage_enum = getattr(sp_resource.Usage, canonical)
        kwargs = {"file_path": file_path, "resource_usage": usage_enum}
        if name:
            kwargs["name"] = str(name).strip()
        if group:
            kwargs["group"] = str(group).strip()
        resource = sp_resource.import_project_resource(**kwargs)
        rid = resource.identifier()
        return {"success": True, "name": rid.name, "url": rid.url(),
                "file_path": file_path, "usage": canonical}
    except Exception as e:
        return {"error": f"Failed to import resource: {e}"}


def handle_list_project_resources(params):
    """List resources used by the current project."""
    if not sp_project.is_open():
        return {"error": "No project is open."}
    try:
        list_fn = getattr(sp_resource, "list_project_resources", None)
        if callable(list_fn):
            resource_ids = list_fn()
            resources = []
            for rid in resource_ids:
                resources.append({
                    "name": rid.name,
                    "context": rid.context,
                    "url": rid.url(),
                })
            return {"count": len(resources), "resources": resources}

        # Newer Painter builds expose document resource enumeration only via JS.
        sp_js = import_js_module()
        if sp_js is None:
            return {
                "error": (
                    "This version of Substance Painter does not expose "
                    "sp_resource.list_project_resources(), and the JavaScript bridge "
                    "is unavailable."
                )
            }

        result = sp_js.evaluate(
            """
            (function() {
                try {
                    var urls = alg.resources.documentResources() || [];
                    var resources = [];
                    for (var i = 0; i < urls.length; i++) {
                        var url = String(urls[i]);
                        var info = {};
                        try {
                            info = alg.resources.getResourceInfo(url) || {};
                        } catch (inner) {
                            info = {};
                        }
                        resources.push({
                            name: info.guiName || info.name || url,
                            context: info.shelfName || null,
                            type: info.type || null,
                            usages: Array.isArray(info.usages) ? info.usages : [],
                            url: url
                        });
                    }
                    return {count: resources.length, resources: resources};
                } catch (e) {
                    return {error: "JS document resource query failed: " + String(e)};
                }
            })()
            """
        )
        if isinstance(result, dict):
            return result
        return {"count": 0, "resources": [], "raw_result": result}
    except Exception as e:
        return {"error": f"Failed to list project resources: {e}"}


# ---------------------------------------------------------------------------
# Projection, fill-parameters and mask-fill handlers
# (knowledge baked in from live API exploration — quirks documented inline)
# ---------------------------------------------------------------------------

# Valid projection modes (sp_layerstack.ProjectionMode members).
# NOTE: enum is not iterable; probe with hasattr. "TriPlanar" does NOT exist.
_PROJECTION_MODE_MAP = {
    "uv":          "UV",
    "triplanar":   "Triplanar",
    "tri-planar":  "Triplanar",
    "tri_planar":  "Triplanar",
    "planar":      "Planar",
    "spherical":   "Spherical",
    "cylindrical": "Cylindrical",
}


def handle_set_layer_projection(params):
    """
    Set the projection mode on any layer node (fill, paint, effect).
    Triplanar is the seam-free choice for tiled textures.
    """
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    mode_str = str(params.get("projection", "")).strip().lower()
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    if not mode_str:
        return {"error": f"'projection' is required. Options: {sorted(set(_PROJECTION_MODE_MAP.values()))}"}
    canonical = _PROJECTION_MODE_MAP.get(mode_str)
    if canonical is None:
        return {"error": f"Unknown projection '{mode_str}'. Options: {sorted(set(_PROJECTION_MODE_MAP.values()))}"}
    pm = getattr(sp_layerstack.ProjectionMode, canonical, None)
    if pm is None:
        return {"error": f"ProjectionMode.{canonical} is not available in this version of Substance Painter."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    # Search both the main stack and mask contents
    nodes = _get_root_nodes(ts.all_stacks()[0])
    node = _find_node_by_name(nodes, layer_name)
    if node is None:
        # Also check inside masks of all layers
        for parent in nodes:
            try:
                mask_nodes_raw, _ = _get_mask_nodes(parent)
                node = _find_node_by_name(mask_nodes_raw, layer_name)
                if node:
                    break
            except Exception:
                pass
    if node is None:
        return {"error": f"Layer/effect '{layer_name}' not found in '{ts_name}' (checked main stack and masks)."}
    try:
        with sp_layerstack.ScopedModification("Set Projection"):
            node.set_projection_mode(pm)
        return {"success": True, "layer_name": layer_name,
                "projection": str(node.get_projection_mode())}
    except Exception as e:
        return {"error": f"Failed to set projection: {e}"}


def _get_fill_source(node, in_mask):
    """
    Return the substance source for a fill node, or (None, error_string).
    - Mask (mono-channel) context: use get_source(None).
    - Main stack (multi-channel) context: use get_material_source().
    Plain colour fills that are not in material/substance mode will raise;
    we catch that and return a clear message.
    """
    try:
        if in_mask:
            src = node.get_source(None)
        else:
            src = node.get_material_source()
    except Exception as e:
        msg = str(e)
        if "material edition mode" in msg.lower():
            return None, ("Fill layer is using a plain colour (not a substance/material). "
                          "Only substance-based fills expose tuneable parameters.")
        if "mono channel" in msg.lower() or "multi channel" in msg.lower():
            return None, f"Channel context mismatch: {msg}"
        return None, f"Could not retrieve source: {msg}"
    if not hasattr(src, "get_parameters"):
        return None, ("Source does not expose parameters "
                       "(it may be a uniform color or bitmap, not a substance).")
    return src, None


def handle_get_fill_parameters(params):
    """
    Get the substance parameters of a fill layer or fill effect node.
    - Multi-channel (main layer stack): uses get_material_source().
    - Mono-channel (mask context): uses get_source(None).
    Plain colour fills have no substance parameters — an informative error
    is returned in that case.
    """
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    nodes = _get_root_nodes(ts.all_stacks()[0])
    node = _find_node_by_name(nodes, layer_name)
    in_mask = False
    if node is None:
        for parent in nodes:
            try:
                mask_nodes_raw, _ = _get_mask_nodes(parent)
                node = _find_node_by_name(mask_nodes_raw, layer_name)
                if node:
                    in_mask = True
                    break
            except Exception:
                pass
    if node is None:
        return {"error": f"Layer/effect '{layer_name}' not found in '{ts_name}'."}
    try:
        src, src_err = _get_fill_source(node, in_mask)
        if src_err:
            return {"error": f"Layer '{layer_name}': {src_err}"}
        raw = src.get_parameters()
        params_out = {k: v for k, v in raw.items()}
        return {"layer_name": layer_name, "in_mask": in_mask,
                "parameters": params_out}
    except Exception as e:
        return {"error": f"Failed to get parameters: {e}"}


def handle_set_fill_parameters(params):
    """
    Set one or more substance parameters on a fill layer or fill effect node.
    - Multi-channel (main layer stack): uses get_material_source().
    - Mono-channel (mask context): uses get_source(None).
    Only the provided keys are updated; others are left unchanged.
    """
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    new_params = params.get("parameters")
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    if not isinstance(new_params, dict) or not new_params:
        return {"error": "'parameters' must be a non-empty dict of {param_name: value}."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    nodes = _get_root_nodes(ts.all_stacks()[0])
    node = _find_node_by_name(nodes, layer_name)
    in_mask = False
    if node is None:
        for parent in nodes:
            try:
                mask_nodes_raw, _ = _get_mask_nodes(parent)
                node = _find_node_by_name(mask_nodes_raw, layer_name)
                if node:
                    in_mask = True
                    break
            except Exception:
                pass
    if node is None:
        return {"error": f"Layer/effect '{layer_name}' not found in '{ts_name}'."}
    try:
        src, src_err = _get_fill_source(node, in_mask)
        if src_err:
            return {"error": f"Layer '{layer_name}': {src_err}"}
        current = src.get_parameters()
        unknown = [k for k in new_params if k not in current]
        if unknown:
            return {"error": f"Unknown parameter(s): {unknown}. Available: {sorted(current.keys())}"}
        current.update(new_params)
        src.set_parameters(current)
        updated = src.get_parameters()
        return {"success": True, "layer_name": layer_name,
                "updated": {k: updated[k] for k in new_params}}
    except Exception as e:
        return {"error": f"Failed to set parameters: {e}"}


def handle_add_mask_fill(params):
    """
    Add a fill effect inside the mask of a named layer and optionally set its
    source to a resource (substance/texture URL).

    Key quirk baked in: to target the mask, we use
        InsertPosition.inside_node(layer_node, NodeStack.Mask)
    where layer_node is the FillLayerNode/PaintLayerNode — NOT a Stack object.
    """
    if not sp_project.is_open():
        return {"error": "No project is open."}
    ts_name = params.get("texture_set")
    layer_name = params.get("layer_name", "").strip()
    fill_name = str(params.get("name", "Mask Fill")).strip() or "Mask Fill"
    resource_url = params.get("resource_url", "").strip()
    if not ts_name:
        return {"error": "'texture_set' is required."}
    if not layer_name:
        return {"error": "'layer_name' is required."}
    ts, err = _get_texture_set(ts_name)
    if err:
        return {"error": err}
    nodes = _get_root_nodes(ts.all_stacks()[0])
    layer_node = _find_node_by_name(nodes, layer_name)
    if layer_node is None:
        return {"error": f"Layer '{layer_name}' not found in '{ts_name}'."}
    if not layer_node.has_mask():
        return {"error": f"Layer '{layer_name}' has no mask. Add a mask first with add_layer_mask."}
    try:
        # IMPORTANT: pass the layer NODE (not a Stack) to inside_node so that
        # .uid() is available. NodeStack.Mask targets the mask content stack.
        mask_pos = sp_layerstack.InsertPosition.inside_node(
            layer_node, sp_layerstack.NodeStack.Mask
        )
        with sp_layerstack.ScopedModification("Add Mask Fill"):
            fill = sp_layerstack.insert_fill(mask_pos)
            fill.set_name(fill_name)
            if resource_url:
                res_id = sp_resource.ResourceID.from_url(resource_url)
                # get_source(None) works in greyscale/mask context
                fill.set_source(None, res_id)
        result = {"success": True, "texture_set": ts_name,
                  "layer_name": layer_name, "fill_name": fill.get_name()}
        if resource_url:
            result["resource_url"] = resource_url
        return result
    except Exception as e:
        return {"error": f"Failed to add mask fill: {e}"}


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

HANDLERS = {
    "get_project_info":             handle_get_project_info,
    "list_texture_sets":            handle_list_texture_sets,
    "export_textures":              handle_export_textures,
    "bake_maps":                    handle_bake_maps,
    "get_export_presets":           handle_get_export_presets,
    "execute_code":                 handle_execute_code,
    "save_project":                 handle_save_project,
    "reload_mesh":                  handle_reload_mesh,
    "get_project_status":           handle_get_project_status,
    "set_texture_set_resolution":   handle_set_texture_set_resolution,
    "list_channels":                handle_list_channels,
    "add_channel":                  handle_add_channel,
    "remove_channel":               handle_remove_channel,
    # Layer stack
    "list_layers":                  handle_list_layers,
    "add_fill_layer":               handle_add_fill_layer,
    "add_paint_layer":              handle_add_paint_layer,
    "add_group_layer":              handle_add_group_layer,
    "rename_layer":                 handle_rename_layer,
    "set_layer_enabled":            handle_set_layer_enabled,
    "delete_layer":                 handle_delete_layer,
    "add_layer_mask":               handle_add_layer_mask,
    "remove_layer_mask":            handle_remove_layer_mask,
    "set_fill_layer_color":         handle_set_fill_layer_color,
    # Resources
    "search_resources":             handle_search_resources,
    # Display & camera
    "get_camera":               handle_get_camera,
    "set_camera":               handle_set_camera,
    "get_environment_map":      handle_get_environment_map,
    "set_environment_map":      handle_set_environment_map,
    "get_tone_mapping":         handle_get_tone_mapping,
    "set_tone_mapping":         handle_set_tone_mapping,
    "get_color_lut":            handle_get_color_lut,
    "set_color_lut":            handle_set_color_lut,
    # Layer enhancements
    "set_layer_opacity":        handle_set_layer_opacity,
    "set_layer_blending_mode":  handle_set_layer_blending_mode,
    "duplicate_layer":          handle_duplicate_layer,
    "move_layer":               handle_move_layer,
    # Project & scene
    "get_scene_bounding_box":   handle_get_scene_bounding_box,
    "get_project_metadata":     handle_get_project_metadata,
    "set_project_metadata":     handle_set_project_metadata,
    "list_project_metadata":    handle_list_project_metadata,
    # Texture set extras
    "get_uv_tiles":             handle_get_uv_tiles,
    "get_mesh_names":           handle_get_mesh_names,
    # Resource management
    "list_shelves":             handle_list_shelves,
    "import_resource":          handle_import_resource,
    "list_project_resources":   handle_list_project_resources,
    # Projection, fill parameters, mask fills
    "set_layer_projection":     handle_set_layer_projection,
    "get_fill_parameters":      handle_get_fill_parameters,
    "set_fill_parameters":      handle_set_fill_parameters,
    "add_mask_fill":            handle_add_mask_fill,
    # Display channel
    "set_display_channel":      handle_set_display_channel,
}


# ---------------------------------------------------------------------------
# TCP socket server (runs on a background thread)
# ---------------------------------------------------------------------------

class PainterSocketServer:
    def __init__(self, host=HOST, port=PORT):
        self.host = host
        self.port = port
        self.running = False
        self._sock = None
        self._thread = None
        self._conn_semaphore = threading.Semaphore(MAX_CONCURRENT_CONNECTIONS)

    def start(self):
        if self.running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                # Prevent port hijacking on Windows.
                # SO_EXCLUSIVEADDRUSE = ~SO_REUSEADDR = ~0x0004 = 0xFFFFFFFB (-5).
                # Use the socket module constant when available (Python 3.12+).
                SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", 0xFFFFFFFB)
                sock.setsockopt(socket.SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(16)
        except Exception:
            sock.close()
            raise

        self._sock = sock
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log_info(f"Socket server listening on {self.host}:{self.port}")

    def stop(self):
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            finally:
                self._sock = None
        self._thread = None
        log_info("Socket server stopped.")

    def _loop(self):
        self._sock.settimeout(1.0)
        while self.running:
            try:
                conn, addr = self._sock.accept()
                if self._conn_semaphore.acquire(blocking=False):
                    threading.Thread(target=self._handle_conn_wrapper, args=(conn,), daemon=True).start()
                else:
                    conn.close()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_conn_wrapper(self, conn):
        try:
            self._handle_conn(conn)
        finally:
            self._conn_semaphore.release()

    def _handle_conn(self, conn):
        with conn:
            conn.settimeout(CLIENT_RECV_TIMEOUT)
            data = b""
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > MAX_REQUEST_SIZE:
                        result = {"error": f"Request too large (>{MAX_REQUEST_SIZE} bytes)."}
                        conn.sendall((json.dumps(result) + "\n").encode("utf-8"))
                        return
                    if data.endswith(b"\n"):
                        break
            except socket.timeout:
                result = {"error": "Client timed out while sending request."}
                try:
                    conn.sendall((json.dumps(result) + "\n").encode("utf-8"))
                except OSError:
                    pass
                return

            if not data:
                return

            try:
                message = json.loads(data.decode("utf-8").strip())
                command = message.get("command")
                params  = message.get("params", {})

                handler = HANDLERS.get(command)
                if handler:
                    result = run_handler_on_painter_thread(handler, params)
                else:
                    result = {"error": f"Unknown command: '{command}'."}
            except Exception as e:
                result = {"error": f"Failed to process request: {e}"}

            response = json.dumps(result) + "\n"
            if len(response) > MAX_RESPONSE_SIZE:
                response = json.dumps({"error": "Response too large to send."}) + "\n"
            conn.sendall(response.encode("utf-8"))


class PainterFileQueueServer:
    def __init__(self, requests_dir=FILE_QUEUE_REQUESTS_DIR, responses_dir=FILE_QUEUE_RESPONSES_DIR):
        self.requests_dir = requests_dir
        self.responses_dir = responses_dir
        self.running = False
        self._thread = None

    def start(self):
        if self.running:
            return
        os.makedirs(self.requests_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log_info(f"File queue server watching {self.requests_dir}")

    def stop(self):
        self.running = False
        self._thread = None
        log_info("File queue server stopped.")

    def _loop(self):
        while self.running:
            try:
                request_names = sorted(
                    name for name in os.listdir(self.requests_dir)
                    if name.endswith(".json")
                )
            except OSError:
                time.sleep(FILE_QUEUE_POLL_INTERVAL)
                continue

            for name in request_names:
                if not self.running:
                    break
                request_path = os.path.join(self.requests_dir, name)
                processing_path = request_path + ".processing"
                try:
                    os.replace(request_path, processing_path)
                except OSError:
                    continue
                self._process_request(processing_path, name)

            time.sleep(FILE_QUEUE_POLL_INTERVAL)

    def _process_request(self, processing_path, original_name):
        request_id = os.path.splitext(original_name)[0]
        try:
            with open(processing_path, "r", encoding="utf-8") as handle:
                message = json.load(handle)
            command = message.get("command")
            params = message.get("params", {})
            request_id = str(message.get("id") or request_id)
            handler = HANDLERS.get(command)
            if handler is None:
                result = {"error": f"Unknown command: '{command}'."}
            else:
                result = run_handler_on_painter_thread(handler, params)
        except Exception as exc:
            result = {"error": f"Failed to process file-queue request: {exc}"}

        response_path = os.path.join(self.responses_dir, f"{request_id}.json")
        temp_path = response_path + ".tmp"
        payload = json.dumps(result)
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp_path, response_path)
        finally:
            try:
                os.remove(processing_path)
            except OSError:
                pass
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Plugin lifecycle (Painter calls start_plugin / close_plugin)
# ---------------------------------------------------------------------------

_server = PainterSocketServer()
_file_queue_server = PainterFileQueueServer()


def start_plugin():
    """Called by Painter when the plugin is enabled."""
    get_handler_dispatcher()
    _server.start()
    _file_queue_server.start()

    # Add a small status widget to Painter's UI so you can see the server state
    status_widget = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(status_widget)
    layout.setContentsMargins(8, 4, 8, 4)
    label = QtWidgets.QLabel(f"🟢 MCP socket {PORT} + file queue active")
    label.setStyleSheet("color: #4CAF50; font-size: 11px;")
    layout.addWidget(label)
    sp_ui.add_dock_widget(status_widget)
    PLUGIN_WIDGETS.append(status_widget)
    log_info("Plugin started.")


def close_plugin():
    """Called by Painter when the plugin is disabled."""
    _server.stop()
    _file_queue_server.stop()
    for w in PLUGIN_WIDGETS:
        remove_widget(w)
    PLUGIN_WIDGETS.clear()
    log_info("Plugin stopped.")


startPlugin = start_plugin
closePlugin = close_plugin
