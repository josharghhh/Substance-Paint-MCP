# Substance Painter MCP

Connect AI assistants to Substance 3D Painter via the [Model Context Protocol](https://modelcontextprotocol.io). Control layers, materials, texture sets, export, baking, camera, and more — all from natural language.

## How it works

Two components communicate over a local TCP socket:

```
AI Client  ──stdio──  server.py (MCP server)  ──TCP :9877──  painter_plugin.py (inside Painter)
```

`server.py` is the MCP server your AI client connects to. `painter_plugin.py` runs inside Painter's embedded Python interpreter and executes all Painter API calls on the main Qt thread.

---

## Setup

### Step 1 — Install the Painter plugin

Copy `painter_plugin.py` into your Painter plugins folder:

**Windows:**
```
%USERPROFILE%\Documents\Adobe\Adobe Substance 3D Painter\python\plugins\
```

**macOS:**
```
~/Documents/Adobe/Adobe Substance 3D Painter/python/plugins/
```

**Linux:**
```
~/Documents/Adobe/Adobe Substance 3D Painter/python/plugins/
```

> If you update `painter_plugin.py`, you must **fully restart Substance Painter** (not just reload the plugin) for changes to take effect.

### Step 2 — Enable the plugin in Painter

Launch Substance Painter and go to **Python > painter_plugin**.

A green status bar should appear at the bottom confirming the TCP server is running on port 9877.

### Step 3 — Install MCP server dependencies

```bash
pip install mcp
```

### Step 4 — Connect your AI client

Choose the client you're using:

---

#### Claude Desktop

Edit your config file:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "substance-painter": {
      "command": "python",
      "args": ["C:/absolute/path/to/server.py"]
    }
  }
}
```

Restart Claude Desktop. The Substance Painter tools will appear automatically.

---

#### GitHub Copilot CLI (VS Code)

Open your VS Code settings (`Ctrl+,`) and search for **MCP**, or edit `settings.json` directly:

```json
{
  "mcp": {
    "servers": {
      "substance-painter": {
        "type": "stdio",
        "command": "python",
        "args": ["C:/absolute/path/to/server.py"]
      }
    }
  }
}
```

Or add it via the VS Code command palette: **MCP: Add Server → stdio → python → path/to/server.py**.

---

#### Cursor

Edit `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "substance-painter": {
      "command": "python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

Restart Cursor or reload the window.

---

#### Windsurf (Codeium)

Edit `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "substance-painter": {
      "command": "python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

---

#### Continue (VS Code / JetBrains extension)

Add to your `~/.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "substance-painter",
      "command": "python",
      "args": ["/absolute/path/to/server.py"]
    }
  ]
}
```

---

#### Zed

Add to your Zed `settings.json`:

```json
{
  "context_servers": {
    "substance-painter": {
      "command": {
        "path": "python",
        "args": ["/absolute/path/to/server.py"]
      }
    }
  }
}
```

---

#### Any MCP-compatible client

`server.py` is a standard stdio MCP server. Point any MCP client at it with:

```
command: python
args:    ["/path/to/server.py"]
type:    stdio
```

---

## Usage examples

With Painter open and the plugin enabled:

- *"Add a fill layer named Base Metal with a metallic gray color"*
- *"Apply a cotton fabric smart material to the Clothing texture set"*
- *"Export all texture sets to ~/Desktop/textures using the Unreal Engine 4 preset"*
- *"Bake the normal and AO maps for the Body texture set at 4096×4096"*
- *"Set the roughness of the Top Layer to 0.3"*
- *"Duplicate the Base Color layer and move it above the Dirt layer"*
- *"Add a black mask to the Scratches layer"*
- *"Set the camera to look at the front of the mesh"*
- *"List all export presets"*
- *"Save the project"*

---

## Available tools (45)

### Project
| Tool | Description |
|------|-------------|
| `get_project_info` | Project path, texture sets, channels, resolution |
| `get_project_status` | Whether a project is open and has unsaved changes |
| `save_project` | Save the open project |
| `get_scene_bounding_box` | Scene dimensions, center point, and radius |
| `get_project_metadata` | Read a metadata key from the project |
| `set_project_metadata` | Write a metadata key/value to the project |
| `list_project_metadata` | List all metadata keys in the project |

### Texture Sets
| Tool | Description |
|------|-------------|
| `list_texture_sets` | All texture set names and resolutions |
| `set_texture_set_resolution` | Change resolution (must be power of 2, 64–8192) |
| `list_channels` | Channels on each stack (BaseColor, Roughness, etc.) |
| `add_channel` | Add a channel to a texture set |
| `remove_channel` | Remove a channel from a texture set |
| `get_uv_tiles` | UV tiles with per-tile resolution and mesh assignments |
| `get_mesh_names` | Mesh names assigned to a texture set |

### Export & Bake
| Tool | Description |
|------|-------------|
| `export_textures` | Export using any named preset |
| `bake_maps` | Bake normal/AO/curvature/position/thickness/ID maps |
| `get_export_presets` | List all available export presets |

### Layer Stack
| Tool | Description |
|------|-------------|
| `list_layers` | Full layer tree (names, types, enabled state) |
| `add_fill_layer` | Add a fill layer |
| `add_paint_layer` | Add a paint layer |
| `add_group_layer` | Add a group (folder) layer |
| `rename_layer` | Rename a layer |
| `set_layer_enabled` | Enable or disable a layer |
| `delete_layer` | Delete a layer |
| `duplicate_layer` | Duplicate a layer |
| `move_layer` | Move a layer above or below another |
| `set_layer_opacity` | Set layer opacity (0.0–1.0) |
| `set_layer_blending_mode` | Set blending mode (Normal, Multiply, Screen, etc.) |
| `add_layer_mask` | Add a black or white mask to a layer |
| `remove_layer_mask` | Remove a mask from a layer |
| `set_fill_layer_color` | Set the base color of a fill layer |
| `set_layer_projection` | Set projection mode (UV, Triplanar, Planar, etc.) |
| `get_fill_parameters` | Get substance/generator parameters of a fill layer |
| `set_fill_parameters` | Set substance/generator parameters on a fill layer |
| `add_mask_fill` | Add a fill effect inside a layer's mask |

### Display & Camera
| Tool | Description |
|------|-------------|
| `get_camera` | Camera position, rotation, FOV, projection |
| `set_camera` | Move/rotate/configure the camera |
| `get_environment_map` | Current environment map resource URL |
| `set_environment_map` | Change the environment map |
| `get_tone_mapping` | Current tone mapping function (Linear/ACES) |
| `set_tone_mapping` | Set tone mapping function |
| `get_color_lut` | Current color LUT resource URL |
| `set_color_lut` | Apply a color LUT |

### Resources
| Tool | Description |
|------|-------------|
| `search_resources` | Search for materials, alphas, smart materials, etc. |
| `list_shelves` | List all resource shelves with paths |
| `import_resource` | Import a texture or material file into the project |
| `list_project_resources` | List all resources used by the project |

### Advanced
| Tool | Description |
|------|-------------|
| `execute_python` | Run sandboxed Python code inside Painter (see security notes) |

---

## Security

The `execute_python` tool runs code inside a three-layer sandbox:

1. **AST validation** — blocks `import`, `eval`, `exec`, `getattr`, `setattr`, dunder access, and filesystem/project-lifecycle operations before any code runs.
2. **Restricted builtins** — only an explicit allowlist of safe builtins is available.
3. **Length limit** — 10,000 character maximum.

The TCP socket server uses `SO_EXCLUSIVEADDRUSE` on Windows, limits concurrent connections to 10, enforces a 30-second receive timeout, and caps request/response sizes at 1 MB / 10 MB.

---

## Troubleshooting

**"Could not connect to Substance Painter"**
- Make sure Painter is open with a project loaded
- Check that the plugin is enabled: **Python > painter_plugin** should have a checkmark
- Confirm nothing else is using port 9877
- Check Painter's Python console for errors

**Plugin doesn't appear in the Python menu**
- Verify the file is in the `python/plugins/` folder (not a subfolder)
- Restart Painter after copying the file

**Tools don't appear in your AI client**
- Restart the AI client after editing its config
- Check the path in the config — it must be absolute, not relative
- Confirm `pip install mcp` completed without errors
- Make sure you're using Python 3.10+

**"Handler failed" or unexpected errors**
- After updating `painter_plugin.py`, do a full Painter restart (not just plugin reload)
- Check Painter's Python console (Window > Views > Python Console) for tracebacks

**Baking doesn't work**
- `bake_maps` requires a high-poly mesh configured in your project's bake settings
- Try running a manual bake in Painter first to confirm the setup is correct

**Layer operations fail on an empty texture set**
- This is fixed in the current version — ensure you have the latest `painter_plugin.py` deployed
