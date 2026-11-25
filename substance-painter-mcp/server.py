"""
Substance Painter MCP Server
=============================
This is the MCP server that Claude Desktop connects to via stdio.
It forwards tool calls to the Painter plugin (painter_plugin.py) over TCP.

Install deps:
  pip install mcp

Run (for testing):
  python server.py

Configure in Claude Desktop's claude_desktop_config.json:
  {
    "mcpServers": {
      "substance-painter": {
        "command": "python",
        "args": ["/path/to/substance_painter_mcp/server.py"]
      }
    }
  }

Make sure Substance Painter is running and the plugin is enabled before
asking Claude to do anything with Painter.
"""

import asyncio
import base64
import json
import re
import socket
import subprocess
import textwrap
import uuid
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# ---------------------------------------------------------------------------
# Connection to Painter plugin
# ---------------------------------------------------------------------------

PAINTER_PORT = 9877
CONNECT_TIMEOUT = 2  # seconds — fail over quickly between candidate hosts
SOCKET_TIMEOUT = 120  # seconds — baking and exporting can take a while
MAX_RESPONSE_SIZE = 10_485_760  # 10 MB
FILE_QUEUE_TIMEOUT = 120  # seconds


def get_candidate_hosts() -> list[str]:
    hosts = ["127.0.0.1"]
    try:
        result = subprocess.run(
            ["ip", "route"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            match = re.match(r"default via (\S+)", line)
            if match:
                host = match.group(1)
                if host not in hosts:
                    hosts.append(host)
                break
    except OSError:
        pass
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"nameserver\s+(\S+)", line)
                if match:
                    host = match.group(1)
                    if host not in hosts:
                        hosts.append(host)
                    break
    except OSError:
        pass
    if "10.255.255.254" not in hosts:
        hosts.append("10.255.255.254")
    return hosts


def send_to_painter_via_windows_loopback(message: str) -> dict:
    encoded = base64.b64encode(message.encode("utf-8")).decode("ascii")
    script = textwrap.dedent(
        f"""
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('127.0.0.1', {PAINTER_PORT})
        $stream = $client.GetStream()
        $writer = New-Object System.IO.StreamWriter($stream)
        $writer.AutoFlush = $true
        $writer.Write([System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')))
        $reader = New-Object System.IO.StreamReader($stream)
        $response = $reader.ReadLine()
        Write-Output $response
        $client.Close()
        """
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        return {"transport_error": f"Windows loopback fallback failed: {stderr}"}
    output = result.stdout.strip()
    if not output:
        return {"transport_error": "Windows loopback fallback returned no data."}
    try:
        return {"payload": json.loads(output)}
    except json.JSONDecodeError as exc:
        return {"transport_error": f"Windows loopback fallback returned invalid JSON: {exc}"}


def send_to_painter_via_file_queue(command: str, params: dict) -> dict:
    request_id = uuid.uuid4().hex
    message = json.dumps({"id": request_id, "command": command, "params": params})
    encoded = base64.b64encode(message.encode("utf-8")).decode("ascii")
    script = textwrap.dedent(
        f"""
        $root = Join-Path ([System.IO.Path]::GetTempPath()) 'SubstancePainterMCP'
        $requests = Join-Path $root 'requests'
        $responses = Join-Path $root 'responses'
        New-Item -ItemType Directory -Force -Path $requests | Out-Null
        New-Item -ItemType Directory -Force -Path $responses | Out-Null
        $requestId = '{request_id}'
        $requestPath = Join-Path $requests ($requestId + '.json')
        $tempPath = Join-Path $requests ($requestId + '.tmp')
        $responsePath = Join-Path $responses ($requestId + '.json')
        $payload = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
        [System.IO.File]::WriteAllText($tempPath, $payload, [System.Text.Encoding]::UTF8)
        Move-Item -Force $tempPath $requestPath
        $deadline = (Get-Date).AddSeconds({FILE_QUEUE_TIMEOUT})
        while ((Get-Date) -lt $deadline) {{
            if (Test-Path $responsePath) {{
                $response = [System.IO.File]::ReadAllText($responsePath, [System.Text.Encoding]::UTF8)
                Remove-Item -Force $responsePath
                Write-Output $response
                exit 0
            }}
            Start-Sleep -Milliseconds 200
        }}
        Write-Error 'Timed out waiting for Substance Painter file-queue response.'
        exit 1
        """
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        return {"transport_error": f"File queue fallback failed: {stderr}"}
    output = result.stdout.strip()
    if not output:
        return {"transport_error": "File queue fallback returned no data."}
    try:
        return {"payload": json.loads(output)}
    except json.JSONDecodeError as exc:
        return {"transport_error": f"File queue fallback returned invalid JSON: {exc}"}


def send_to_painter(command: str, params: dict) -> dict:
    """Send a JSON command to the Painter plugin and return the response."""
    message = json.dumps({"command": command, "params": params}) + "\n"
    last_error = None
    for host in get_candidate_hosts():
        try:
            with socket.create_connection((host, PAINTER_PORT), timeout=CONNECT_TIMEOUT) as sock:
                sock.settimeout(SOCKET_TIMEOUT)
                sock.sendall(message.encode("utf-8"))
                response_data = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response_data += chunk
                    if len(response_data) > MAX_RESPONSE_SIZE:
                        return {"error": "Response from Painter exceeded size limit."}
                    if response_data.endswith(b"\n"):
                        break
            return json.loads(response_data.decode("utf-8").strip())
        except (ConnectionRefusedError, OSError) as exc:
            last_error = exc
            continue
        except socket.timeout:
            return {"error": "Request timed out — the operation may still be running in Painter."}
        except Exception as exc:
            return {"error": f"Communication error: {exc}"}

    fallback = send_to_painter_via_windows_loopback(message)
    if "payload" in fallback:
        return fallback["payload"]

    file_queue_fallback = send_to_painter_via_file_queue(command, params)
    if "payload" in file_queue_fallback:
        return file_queue_fallback["payload"]

    return {
        "error": (
            "Could not connect to Substance Painter. "
            "Make sure Painter is open and the MCP plugin is enabled "
            f"(Python > painter_plugin). Listening on port {PAINTER_PORT}. "
            f"Last socket error: {last_error}. "
            f"Socket fallback error: {fallback['transport_error']}. "
            f"File queue fallback error: {file_queue_fallback['transport_error']}"
        )
    }


def sanitize_response(result: dict) -> dict:
    """Remove internal traceback details from error responses sent to MCP clients."""
    if "traceback" in result:
        del result["traceback"]
    return result


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------

server = Server("substance-painter")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_project_info",
            description=(
                "Get information about the currently open Substance Painter project: "
                "file path, texture sets, channel types, and resolution."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="list_texture_sets",
            description="List all texture sets in the open Painter project with their names and resolutions.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="export_textures",
            description=(
                "Export textures from the open Painter project using a named export preset. "
                "Can export all texture sets or a specific subset."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "preset_name": {
                        "type": "string",
                        "description": "Export preset name, e.g. 'PBR Metallic Roughness', 'Unreal Engine 4', 'Unity HD Render Pipeline (Metallic Standard)'",
                        "default": "PBR Metallic Roughness",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Absolute path to the output directory where textures will be saved.",
                    },
                    "texture_sets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of texture set names to export. Exports all sets if omitted.",
                    },
                },
                "required": ["output_path"],
            },
        ),
        types.Tool(
            name="bake_maps",
            description=(
                "Bake texture maps (normal, AO, curvature, etc.) for a texture set in Painter. "
                "Requires a mesh with high-poly baking set up in the project."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {
                        "type": "string",
                        "description": "Name of the texture set to bake maps for.",
                    },
                    "maps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Map types to bake: normal, ao, curvature, position, thickness, id.",
                        "default": ["normal", "ao", "curvature"],
                    },
                    "resolution": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Bake resolution as [width, height], e.g. [2048, 2048].",
                        "default": [2048, 2048],
                    },
                },
                "required": ["texture_set"],
            },
        ),
        types.Tool(
            name="bake_info",
            description=(
                "Native Python baking API: list the bake properties (common + per baker, current values, "
                "enum options) for a texture set. Use before bake_start to learn property names."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Texture set (default: first)."},
                    "bakers": {"type": "array", "items": {"type": "string"},
                               "description": "Bakers to describe, e.g. ['ID', 'AO'] (default: enabled ones)."},
                },
            },
        ),
        types.Tool(
            name="bake_start",
            description=(
                "Native Python bake (async, returns immediately; poll bake_status). Sets output size, enabled "
                "bakers and any property, e.g. set={'common': {'LowAsHigh': true}, 'ID': {'ColorSource': 'Vertex Color'}}. "
                "Enum values may be given by name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_sets": {"type": "array", "items": {"type": "string"}, "description": "Default: all."},
                    "size": {"type": "integer", "description": "Output size in px (power of 2), e.g. 4096."},
                    "bakers": {"type": "array", "items": {"type": "string"},
                               "description": "Enabled bakers, e.g. ['Normal','WorldSpaceNormal','ID','AO','Curvature','Position','Thickness']."},
                    "set": {"type": "object", "description": "{group: {property: value}}; group 'common' or a baker name."},
                },
            },
        ),
        types.Tool(
            name="reload_plugin",
            description="Re-import painter_plugin.py from disk inside Painter (after updating the plugin file).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="reload_mesh_status",
            description="Status of the last reload_mesh (reload is async): RUNNING, SUCCESS or ERROR.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="bake_status",
            description="Status of the last bake_start: running flag, elapsed seconds, end status.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_export_presets",
            description="List all available export presets in Substance Painter.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="save_project",
            description="Save the currently open Substance Painter project.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="reload_mesh",
            description=(
                "Reload the current project mesh from a file path and wait for Painter's "
                "reload callback. Useful after re-exporting the source FBX from Blender."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mesh_file_path": {
                        "type": "string",
                        "description": "Absolute path to the mesh file (.fbx, .obj, .dae, .ply).",
                    },
                    "import_cameras": {
                        "type": "boolean",
                        "description": "Whether to import cameras from the mesh file.",
                        "default": True,
                    },
                    "preserve_strokes": {
                        "type": "boolean",
                        "description": "Whether to preserve existing stroke positions on the mesh.",
                        "default": True,
                    },
                },
                "required": ["mesh_file_path"],
            },
        ),
        types.Tool(
            name="execute_python",
            description=(
                "Execute arbitrary Python code inside Substance Painter. "
                "Gives direct access to the full substance_painter API. "
                "Use for tasks not covered by the other tools. "
                "Available modules: sp_project, sp_export, sp_textureset, sp_resource, sp_logging, sp_layerstack, sp_colormanagement, sp_display, sp_js (JS bridge, may be None)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python source code to execute inside Painter.",
                    },
                },
                "required": ["code"],
            },
        ),
        types.Tool(
            name="get_project_status",
            description="Return whether a project is open, saved, has unsaved changes, and its file path.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="set_texture_set_resolution",
            description="Change the resolution of a texture set. Width and height must be powers of two between 64 and 8192.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "width":       {"type": "integer", "description": "New width in pixels (64–8192, power of two)."},
                    "height":      {"type": "integer", "description": "New height in pixels (64–8192, power of two)."},
                },
                "required": ["texture_set", "width", "height"],
            },
        ),
        types.Tool(
            name="list_channels",
            description="List channels on each stack of one or all texture sets (BaseColor, Roughness, Metallic, Normal, Height, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Specific texture set to query. Omit for all."},
                },
            },
        ),
        types.Tool(
            name="add_channel",
            description="Add a channel to the default stack of a texture set.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set":  {"type": "string", "description": "Name of the texture set."},
                    "channel_type": {"type": "string", "description": "Channel to add, e.g. 'Emissive', 'AmbientOcclusion'. Case-insensitive."},
                },
                "required": ["texture_set", "channel_type"],
            },
        ),
        types.Tool(
            name="remove_channel",
            description="Remove a channel from the default stack of a texture set.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set":  {"type": "string", "description": "Name of the texture set."},
                    "channel_type": {"type": "string", "description": "Channel to remove. Case-insensitive."},
                },
                "required": ["texture_set", "channel_type"],
            },
        ),
        # ---- Layer stack ----
        types.Tool(
            name="list_layers",
            description="List layers in the default stack of a texture set, returning a tree of layer names, types, and enabled state.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set to inspect."},
                },
                "required": ["texture_set"],
            },
        ),
        types.Tool(
            name="add_fill_layer",
            description="Add a fill layer to the default stack of a texture set, optionally above a named layer.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set":   {"type": "string", "description": "Name of the texture set."},
                    "name":          {"type": "string", "description": "Name for the new fill layer.", "default": "Fill Layer"},
                    "above_layer":   {"type": "string", "description": "Name of an existing layer to insert above. Omit to insert at the top."},
                },
                "required": ["texture_set"],
            },
        ),
        types.Tool(
            name="add_paint_layer",
            description="Add a paint layer to the default stack of a texture set, optionally above a named layer.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set":   {"type": "string", "description": "Name of the texture set."},
                    "name":          {"type": "string", "description": "Name for the new paint layer.", "default": "Paint Layer"},
                    "above_layer":   {"type": "string", "description": "Name of an existing layer to insert above. Omit to insert at the top."},
                },
                "required": ["texture_set"],
            },
        ),
        types.Tool(
            name="add_group_layer",
            description="Add a group (folder) layer to the default stack of a texture set, optionally above a named layer.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set":   {"type": "string", "description": "Name of the texture set."},
                    "name":          {"type": "string", "description": "Name for the new group layer.", "default": "Group"},
                    "above_layer":   {"type": "string", "description": "Name of an existing layer to insert above. Omit to insert at the top."},
                },
                "required": ["texture_set"],
            },
        ),
        types.Tool(
            name="rename_layer",
            description="Rename a layer in a texture set's default stack.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Current name of the layer to rename."},
                    "new_name":    {"type": "string", "description": "New name to assign."},
                },
                "required": ["texture_set", "layer_name", "new_name"],
            },
        ),
        types.Tool(
            name="set_layer_enabled",
            description="Enable or disable a layer in a texture set's default stack.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Name of the layer to toggle."},
                    "enabled":     {"type": "boolean", "description": "True to enable, false to disable."},
                },
                "required": ["texture_set", "layer_name", "enabled"],
            },
        ),
        types.Tool(
            name="delete_layer",
            description="Delete a layer from a texture set's default stack.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Name of the layer to delete."},
                },
                "required": ["texture_set", "layer_name"],
            },
        ),
        types.Tool(
            name="add_layer_mask",
            description="Add a mask to a layer in a texture set's default stack.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Name of the layer to mask."},
                    "background":  {"type": "string", "description": "'White' (fully revealed) or 'Black' (fully hidden).", "default": "White"},
                },
                "required": ["texture_set", "layer_name"],
            },
        ),
        types.Tool(
            name="remove_layer_mask",
            description="Remove the mask from a layer in a texture set's default stack.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Name of the layer whose mask to remove."},
                },
                "required": ["texture_set", "layer_name"],
            },
        ),
        types.Tool(
            name="set_fill_layer_color",
            description=(
                "Set the fill color of a fill layer's channel (e.g. basecolor). "
                "Color is specified as [r, g, b] floats in the 0–1 range."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Name of the fill layer to modify."},
                    "color":       {"type": "array", "items": {"type": "number"}, "description": "[r, g, b] floats in 0–1 range."},
                    "channel":     {"type": "string", "description": "Channel to set color on, e.g. 'basecolor'.", "default": "basecolor"},
                },
                "required": ["texture_set", "layer_name", "color"],
            },
        ),
        # ---- Resources ----
        types.Tool(
            name="search_resources",
            description="Search Substance Painter resources (materials, alphas, textures, smart materials, etc.) by name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term to match against resource names."},
                    "type":  {"type": "string", "description": "Optional resource type filter: material, alpha, texture, brush, filter, generator, smart-material, smart-mask, export-preset."},
                },
                "required": ["query"],
            },
        ),
        # ---- Display & Camera ----
        types.Tool(
            name="get_camera",
            description="Get the default camera properties: position, rotation, FOV, focal length, focus distance, aperture, projection type.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="set_camera",
            description="Set camera properties. Only provide the fields you want to change.",
            inputSchema={
                "type": "object",
                "properties": {
                    "position":       {"type": "array", "items": {"type": "number"}, "description": "[x, y, z] position in scene space."},
                    "rotation":       {"type": "array", "items": {"type": "number"}, "description": "[x, y, z] Euler rotation in degrees."},
                    "field_of_view":  {"type": "number", "description": "Field of view in degrees (3–179)."},
                    "focal_length":   {"type": "number", "description": "Focal length in mm (1–500)."},
                    "focus_distance": {"type": "number", "description": "Focus distance."},
                    "aperture":       {"type": "number", "description": "Aperture (depth of field width)."},
                    "projection_type":      {"type": "string", "description": "'Perspective' or 'Orthographic'."},
                    "orthographic_height":  {"type": "number", "description": "Orthographic height (only for orthographic projection)."},
                },
            },
        ),
        types.Tool(
            name="get_environment_map",
            description="Get the current environment map resource URL used for scene lighting.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="set_environment_map",
            description="Set the environment map by resource URL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Resource URL of the environment map."},
                },
                "required": ["url"],
            },
        ),
        types.Tool(
            name="get_tone_mapping",
            description="Get the current tone mapping function (Linear or ACES). Unavailable when color management is enabled.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="set_tone_mapping",
            description="Set the tone mapping function. Unavailable when color management is enabled.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tone_mapping": {"type": "string", "description": "'Linear' or 'ACES'."},
                },
                "required": ["tone_mapping"],
            },
        ),
        types.Tool(
            name="get_color_lut",
            description="Get the current color profile LUT resource URL.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="set_color_lut",
            description="Set the color profile LUT by resource URL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Resource URL of the color LUT."},
                },
                "required": ["url"],
            },
        ),
        types.Tool(
            name="set_display_channel",
            description=(
                "NOT SUPPORTED: Substance Painter provides no API (Python or JavaScript) "
                "to control the 3D viewport channel display mode. This tool always returns "
                "an error. Use the Solo Channel buttons in the Painter UI directly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": (
                            "Channel to display. Use 'material' for the full PBR view, "
                            "or one of: BaseColor, Roughness, Metallic, Normal, Height, "
                            "Emissive, Opacity, AmbientOcclusion."
                        ),
                    },
                },
                "required": ["channel"],
            },
        ),
        # ---- Layer Enhancements ----
        types.Tool(
            name="set_layer_opacity",
            description="Set the opacity of a layer (0.0 to 1.0).",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Name of the layer."},
                    "opacity":     {"type": "number", "description": "Opacity value from 0.0 (transparent) to 1.0 (opaque)."},
                },
                "required": ["texture_set", "layer_name", "opacity"],
            },
        ),
        types.Tool(
            name="set_layer_blending_mode",
            description="Set the blending mode of a layer (Normal, Multiply, Screen, Overlay, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set":  {"type": "string", "description": "Name of the texture set."},
                    "layer_name":   {"type": "string", "description": "Name of the layer."},
                    "blending_mode": {"type": "string", "description": "Blending mode name, e.g. 'Normal', 'Multiply', 'Screen', 'Overlay', 'SoftLight'. Case-insensitive."},
                },
                "required": ["texture_set", "layer_name", "blending_mode"],
            },
        ),
        types.Tool(
            name="duplicate_layer",
            description="Duplicate a layer in a texture set.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Name of the layer to duplicate."},
                },
                "required": ["texture_set", "layer_name"],
            },
        ),
        types.Tool(
            name="move_layer",
            description="Move a layer above or below another layer in the stack.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set":   {"type": "string", "description": "Name of the texture set."},
                    "layer_name":    {"type": "string", "description": "Name of the layer to move."},
                    "target_layer":  {"type": "string", "description": "Name of the reference layer. Omit to move to top of stack."},
                    "position":      {"type": "string", "description": "'above' or 'below' the target layer.", "default": "above"},
                },
                "required": ["texture_set", "layer_name"],
            },
        ),
        # ---- Project & Scene ----
        types.Tool(
            name="get_scene_bounding_box",
            description="Get the scene bounding box: dimensions, center point, and radius.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_project_metadata",
            description="Get a metadata value from the project by key.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key":     {"type": "string", "description": "Metadata key to retrieve."},
                    "context": {"type": "string", "description": "Metadata context (default: 'project').", "default": "project"},
                },
                "required": ["key"],
            },
        ),
        types.Tool(
            name="set_project_metadata",
            description="Set a metadata key-value pair on the project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key":     {"type": "string", "description": "Metadata key to set."},
                    "value":   {"description": "Value to store (string, number, bool, etc.)."},
                    "context": {"type": "string", "description": "Metadata context (default: 'project').", "default": "project"},
                },
                "required": ["key", "value"],
            },
        ),
        types.Tool(
            name="list_project_metadata",
            description="List all metadata keys in the project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "context": {"type": "string", "description": "Metadata context (default: 'project').", "default": "project"},
                },
            },
        ),
        # ---- Texture Set Extras ----
        types.Tool(
            name="get_uv_tiles",
            description="Get UV tiles for a texture set with per-tile resolution and mesh assignments.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                },
                "required": ["texture_set"],
            },
        ),
        types.Tool(
            name="get_mesh_names",
            description="Get mesh names assigned to a texture set.",
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                },
                "required": ["texture_set"],
            },
        ),
        # ---- Resource Management ----
        types.Tool(
            name="list_shelves",
            description="List all resource shelves with their paths and import capability.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="import_resource",
            description="Import a resource file (texture, alpha, environment, material, etc.) into the project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the resource file."},
                    "usage":     {"type": "string", "description": "Resource usage type: texture, alpha, environment, base_material, smart_material, smart_mask, generator, filter, brush, font, color_lut, export, shader."},
                    "name":      {"type": "string", "description": "Optional custom name for the resource."},
                    "group":     {"type": "string", "description": "Optional group/category."},
                },
                "required": ["file_path", "usage"],
            },
        ),
        types.Tool(
            name="list_project_resources",
            description="List all resources used by the current project.",
            inputSchema={"type": "object", "properties": {}},
        ),
        # ---- Projection & Fill Parameters ----
        types.Tool(
            name="set_layer_projection",
            description=(
                "Set the projection mode on a layer or fill effect. "
                "Use 'Triplanar' for seam-free tiling textures. "
                "Options: UV, Triplanar, Planar, Spherical, Cylindrical."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Name of the layer or fill effect (searches main stack and mask contents)."},
                    "projection":  {"type": "string", "description": "Projection mode: UV, Triplanar, Planar, Spherical, Cylindrical."},
                },
                "required": ["texture_set", "layer_name", "projection"],
            },
        ),
        types.Tool(
            name="get_fill_parameters",
            description=(
                "Get the substance/generator parameters of a fill layer or fill effect node. "
                "Works in both the main layer stack and inside masks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Name of the fill layer or fill effect."},
                },
                "required": ["texture_set", "layer_name"],
            },
        ),
        types.Tool(
            name="set_fill_parameters",
            description=(
                "Set one or more substance/generator parameters on a fill layer or fill effect. "
                "Works in both the main layer stack and inside masks. "
                "Only provided keys are updated; others remain unchanged."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set": {"type": "string", "description": "Name of the texture set."},
                    "layer_name":  {"type": "string", "description": "Name of the fill layer or fill effect."},
                    "parameters":  {"type": "object", "description": "Dict of {param_name: value} to update, e.g. {\"contrast\": 0.8, \"balance\": 0.4}."},
                },
                "required": ["texture_set", "layer_name", "parameters"],
            },
        ),
        types.Tool(
            name="add_mask_fill",
            description=(
                "Add a fill effect inside the mask of a named layer, optionally sourced from a resource URL. "
                "The layer must already have a mask (use add_layer_mask first)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "texture_set":   {"type": "string", "description": "Name of the texture set."},
                    "layer_name":    {"type": "string", "description": "Name of the layer whose mask to fill."},
                    "name":          {"type": "string", "description": "Name for the new fill effect.", "default": "Mask Fill"},
                    "resource_url":  {"type": "string", "description": "Optional resource URL of a substance/texture to use as the fill source, e.g. 'resource://starter_assets/grunge_concrete/grunge_concrete?version=...'"},
                },
                "required": ["texture_set", "layer_name"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    """Route MCP tool calls to the Painter plugin."""

    # Map MCP tool names to painter plugin command names
    command_map = {
        "get_project_info":           "get_project_info",
        "list_texture_sets":          "list_texture_sets",
        "export_textures":            "export_textures",
        "bake_maps":                  "bake_maps",
        "bake_info":                  "bake_info",
        "bake_start":                 "bake_start",
        "bake_status":                "bake_status",
        "reload_mesh_status":         "reload_mesh_status",
        "reload_plugin":              "reload_plugin",
        "get_export_presets":         "get_export_presets",
        "save_project":               "save_project",
        "reload_mesh":                "reload_mesh",
        "execute_python":             "execute_code",
        "get_project_status":         "get_project_status",
        "set_texture_set_resolution": "set_texture_set_resolution",
        "list_channels":              "list_channels",
        "add_channel":                "add_channel",
        "remove_channel":             "remove_channel",
        # Layer stack
        "list_layers":                "list_layers",
        "add_fill_layer":             "add_fill_layer",
        "add_paint_layer":            "add_paint_layer",
        "add_group_layer":            "add_group_layer",
        "rename_layer":               "rename_layer",
        "set_layer_enabled":          "set_layer_enabled",
        "delete_layer":               "delete_layer",
        "add_layer_mask":             "add_layer_mask",
        "remove_layer_mask":          "remove_layer_mask",
        "set_fill_layer_color":       "set_fill_layer_color",
        # Resources
        "search_resources":           "search_resources",
        # Display & camera
        "get_camera":               "get_camera",
        "set_camera":               "set_camera",
        "get_environment_map":      "get_environment_map",
        "set_environment_map":      "set_environment_map",
        "get_tone_mapping":         "get_tone_mapping",
        "set_tone_mapping":         "set_tone_mapping",
        "get_color_lut":            "get_color_lut",
        "set_color_lut":            "set_color_lut",
        "set_display_channel":      "set_display_channel",
        # Layer enhancements
        "set_layer_opacity":        "set_layer_opacity",
        "set_layer_blending_mode":  "set_layer_blending_mode",
        "duplicate_layer":          "duplicate_layer",
        "move_layer":               "move_layer",
        # Project & scene
        "get_scene_bounding_box":   "get_scene_bounding_box",
        "get_project_metadata":     "get_project_metadata",
        "set_project_metadata":     "set_project_metadata",
        "list_project_metadata":    "list_project_metadata",
        # Texture set extras
        "get_uv_tiles":             "get_uv_tiles",
        "get_mesh_names":           "get_mesh_names",
        # Resource management
        "list_shelves":             "list_shelves",
        "import_resource":          "import_resource",
        "list_project_resources":   "list_project_resources",
        # Projection & fill parameters
        "set_layer_projection":     "set_layer_projection",
        "get_fill_parameters":      "get_fill_parameters",
        "set_fill_parameters":      "set_fill_parameters",
        "add_mask_fill":            "add_mask_fill",
    }

    command = command_map.get(name)
    if not command:
        result = {"error": f"Unknown tool: {name}"}
    else:
        # Run the blocking socket call in a thread so we don't block the event loop
        loop = asyncio.get_event_loop()
        params = arguments if arguments else {}

        # Basic input validation
        if name == "export_textures":
            if not isinstance(params.get("output_path"), str) or not params["output_path"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "output_path is required and must be a non-empty string."}))]
        elif name == "bake_maps":
            if not isinstance(params.get("texture_set"), str) or not params["texture_set"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "texture_set is required and must be a non-empty string."}))]
        elif name == "reload_mesh":
            if not isinstance(params.get("mesh_file_path"), str) or not params["mesh_file_path"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "mesh_file_path is required and must be a non-empty string."}))]
            for field in ("import_cameras", "preserve_strokes"):
                if field in params and not isinstance(params.get(field), bool):
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} must be a boolean when provided."}))]
        elif name == "execute_python":
            if not isinstance(params.get("code"), str) or not params["code"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "code is required and must be a non-empty string."}))]
        elif name == "set_texture_set_resolution":
            if not isinstance(params.get("texture_set"), str) or not params["texture_set"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "texture_set is required."}))]
            if not isinstance(params.get("width"), int):
                return [types.TextContent(type="text", text=json.dumps({"error": "width is required and must be an integer."}))]
            if not isinstance(params.get("height"), int):
                return [types.TextContent(type="text", text=json.dumps({"error": "height is required and must be an integer."}))]
        elif name in ("add_channel", "remove_channel"):
            if not isinstance(params.get("texture_set"), str) or not params["texture_set"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "texture_set is required."}))]
            if not isinstance(params.get("channel_type"), str) or not params["channel_type"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "channel_type is required."}))]
        elif name in ("list_layers", "add_fill_layer", "add_paint_layer", "add_group_layer"):
            if not isinstance(params.get("texture_set"), str) or not params["texture_set"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "texture_set is required."}))]
        elif name == "rename_layer":
            for field in ("texture_set", "layer_name", "new_name"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} is required."}))]
        elif name in ("set_layer_enabled",):
            for field in ("texture_set", "layer_name"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} is required."}))]
            if not isinstance(params.get("enabled"), bool):
                return [types.TextContent(type="text", text=json.dumps({"error": "enabled must be a boolean."}))]
        elif name in ("delete_layer", "add_layer_mask", "remove_layer_mask"):
            for field in ("texture_set", "layer_name"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} is required."}))]
        elif name == "set_fill_layer_color":
            for field in ("texture_set", "layer_name"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} is required."}))]
            if not isinstance(params.get("color"), list) or len(params["color"]) != 3:
                return [types.TextContent(type="text", text=json.dumps({"error": "color must be a list of three floats [r, g, b]."}))]
        elif name == "search_resources":
            if not isinstance(params.get("query"), str) or not params["query"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "query is required."}))]
        elif name == "set_environment_map":
            if not isinstance(params.get("url"), str) or not params["url"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "url is required."}))]
        elif name == "set_tone_mapping":
            if not isinstance(params.get("tone_mapping"), str) or not params["tone_mapping"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "tone_mapping is required ('Linear' or 'ACES')."}))]
        elif name == "set_color_lut":
            if not isinstance(params.get("url"), str) or not params["url"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "url is required."}))]
        elif name == "set_display_channel":
            if not isinstance(params.get("channel"), str) or not params["channel"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "channel is required (e.g. 'BaseColor', 'Roughness', 'material')."}))]
        elif name in ("set_layer_opacity", "set_layer_blending_mode"):
            for field in ("texture_set", "layer_name"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} is required."}))]
            if name == "set_layer_opacity":
                if not isinstance(params.get("opacity"), (int, float)):
                    return [types.TextContent(type="text", text=json.dumps({"error": "opacity is required and must be a number."}))]
            elif name == "set_layer_blending_mode":
                if not isinstance(params.get("blending_mode"), str) or not params["blending_mode"].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": "blending_mode is required."}))]
        elif name in ("duplicate_layer", "move_layer"):
            for field in ("texture_set", "layer_name"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} is required."}))]
        elif name == "get_project_metadata":
            if not isinstance(params.get("key"), str) or not params["key"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "key is required."}))]
        elif name == "set_project_metadata":
            if not isinstance(params.get("key"), str) or not params["key"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "key is required."}))]
            if params.get("value") is None:
                return [types.TextContent(type="text", text=json.dumps({"error": "value is required."}))]
        elif name in ("get_uv_tiles", "get_mesh_names"):
            if not isinstance(params.get("texture_set"), str) or not params["texture_set"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "texture_set is required."}))]
        elif name == "import_resource":
            if not isinstance(params.get("file_path"), str) or not params["file_path"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "file_path is required."}))]
            if not isinstance(params.get("usage"), str) or not params["usage"].strip():
                return [types.TextContent(type="text", text=json.dumps({"error": "usage is required."}))]
        elif name == "set_layer_projection":
            for field in ("texture_set", "layer_name", "projection"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} is required."}))]
        elif name in ("get_fill_parameters",):
            for field in ("texture_set", "layer_name"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} is required."}))]
        elif name == "set_fill_parameters":
            for field in ("texture_set", "layer_name"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} is required."}))]
            if not isinstance(params.get("parameters"), dict) or not params["parameters"]:
                return [types.TextContent(type="text", text=json.dumps({"error": "parameters must be a non-empty object."}))]
        elif name == "add_mask_fill":
            for field in ("texture_set", "layer_name"):
                if not isinstance(params.get(field), str) or not params[field].strip():
                    return [types.TextContent(type="text", text=json.dumps({"error": f"{field} is required."}))]

        # execute_python passes code through as-is
        if name == "execute_python":
            params = {"code": arguments.get("code", "")}
        result = await loop.run_in_executor(None, send_to_painter, command, params)

    result = sanitize_response(result)
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
