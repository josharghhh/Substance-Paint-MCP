# Painter MCP Playbook

This file is the operating procedure for live Painter MCP sessions.

## Default Inspection Sequence

Run these first unless the task is extremely narrow:
- `get_project_status`
- `get_project_info`
- `list_texture_sets`
- `list_layers`
- `get_mesh_names`
- `list_channels`

Use this to answer:
- is this a saved `.spp` or a read-only template?
- how many texture sets exist?
- which mesh names map to each texture set?
- what layers already exist?
- are the needed channels present?

## Default Edit Sequence

1. import missing source maps
2. add explicit fill layers with meaningful names
3. wire source maps or procedural materials
4. disable or zero out default starter layers
5. tune per-channel opacity
6. tune substance parameters
7. verify with screenshots
8. save/export if project state allows

## Use `execute_python` Sparingly

Use it when:
- the dedicated MCP tool is missing
- the operation is still Painter-side and safe
- the change is easier to express through Painter API calls than many tool calls

Do not use it to skip inspection.

## Known Operational Risks

- Painter may reopen into a read-only starter template.
- Reloaded meshes may not preserve the same texture-set structure.
- A project may lose imported resources when reopened or replaced.
- The direct MCP transport may be flaky; re-check project state after reconnect.

## Read-Only Template Rule

If `get_project_status` or the window title shows a starter template or read-only state:
- do not assume save will work
- rebuild only what is necessary for review
- tell the user when persistence is blocked by template/read-only state

## Debugging Order

When the result is visually broken:
1. verify project state
2. verify texture sets
3. verify mesh-to-texture-set mapping
4. verify layers
5. verify material response
6. only then export
