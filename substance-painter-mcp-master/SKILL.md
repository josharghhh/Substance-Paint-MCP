---
name: substance-painter-mcp-master
description: Use when working in Adobe Substance 3D Painter through MCP for game-asset texturing, PBR material authoring, Blender-to-Painter handoff, screenshot-driven art direction, and debugging texture set, UV, export, or layer-stack problems. Covers felt, plush, fur-card, stylized creature, and general Painter workflows using Adobe official Painter and PBR guidance as the primary source of truth.
---

# Substance Painter MCP Master

Use this skill for Painter work that needs both artistic judgment and a stable MCP workflow.

Default outcome:
- inspect the live Painter project before editing
- determine whether the problem belongs to Painter, Blender, or export setup
- build or repair a clean PBR stack
- capture screenshots at checkpoints
- export only after the stack and texture sets are verified

## Use This Skill When

Use it for:
- Painter stack setup or repair
- PBR texture authoring and review
- stylized plush, felt, fabric, painted, worn, or creature surface work
- Blender material-slot, UV, or fur-card handoff into Painter
- texture set disappearance, bad export output, shard artifacts, or read-only/template confusion
- screenshot-driven directional review of a texturing pass

Do not use it as the primary workflow for:
- modeling or silhouette fixes that belong in Blender
- Designer graph authoring
- image-generation-only work that does not involve Painter

## Core Rules

- Inspect first. Never assume texture set layout, material slots, UV ownership, or export preset behavior.
- Detect project state immediately. Verify whether the live project is a saved `.spp`, an unsaved template, or a read-only starter template.
- Preserve base truth. Rebuild a clean base stack from imported source maps before adding look-dev layers.
- Keep ownership boundaries clear:
  - Blender owns silhouette, mesh splits, UV sets, material slots, fur-card geometry, and card density.
  - Painter owns surface response, breakup, masking, color treatment, roughness, normal/height tuning, and export validation.
- Treat screenshots as checkpoints, not decoration. Capture before/after when a change affects art direction.
- Prefer Adobe Painter terminology and behavior over memory or guesswork.

## Default MCP Workflow

Always follow this order unless a task is clearly narrower:

1. Inspect project state
   - `get_project_status`
   - `get_project_info`
2. Inspect structure
   - `list_texture_sets`
   - `list_layers`
   - `get_mesh_names`
   - `list_channels`
3. Confirm assumptions
   - identify source maps already present
   - identify whether the project is using the expected mesh/material split
   - identify whether the problem is stack-level, texture-set-level, UV-level, or geometry-level
4. Rebuild or edit the stack
   - import missing maps
   - create explicit fill layers
   - disable legacy/default layers that interfere
5. Tune materials
   - roughness and height first
   - normal conservatively
   - base color only as needed
6. Validate visually
   - capture screenshots
   - check that the current view matches the intended artistic direction
7. Save and export
   - save only if the project is writable
   - validate export preset and output set

## High-Value Decision Rules

### Establish Base Truth

Use this when a project is messy, newly imported, or clearly wrong.

- Verify the live mesh/material-slot result in Painter before touching the stack.
- Import source maps only after confirming which texture set they belong to.
- Build named fill layers such as `base_maps`, `felt_plush`, `fur_cards`, or similar explicit roles.
- Disable or zero out default `Layer 1` paint layers unless they are intentionally used.

Read:
- `references/painter-core-workflow.md`
- `references/painter-mcp-playbook.md`

### Surface Look Development

- Preserve primary color identity.
- Add tactile response with roughness first.
- Add height lightly and normal even more lightly.
- Avoid overdriving microdetail until the asset reads correctly from normal viewing distance.
- If the surface reads noisy, spiky, or crunchy, reduce normal/height before touching base color.

Read:
- `references/pbr-guide.md`
- `references/painter-material-techniques.md`

### Stylized Plush / Felt / Creature Work

- Keep the character's original color read intact unless the prompt explicitly asks for recoloring.
- Use fill-based felt or plush materials to alter response, not to destroy the base identity.
- If fur cards read as shards, spikes, or plates, suspect geometry, UV, or material-slot issues before repainting.
- If the silhouette is wrong, go back to Blender.

Read:
- `references/painter-material-techniques.md`
- `references/painter-game-asset-handoff.md`

### Debugging Broken Results

Use this order:

1. Wrong texture set count or missing texture set:
   - check mesh reload/import result
   - check material slots in Blender/exported mesh
2. Opaque shards / random card faces:
   - check UV assignment and texture-set ownership
   - check whether opacity texture is sampling the correct UV set
   - check whether a default paint layer or opaque layer is still enabled
3. Export output is wrong:
   - validate preset selection
   - validate texture-set inclusion
   - validate channel routing before repainting
4. Asset still looks wrong after stack tweaks:
   - determine whether the cause is geometry or Painter shading

Read:
- `references/painter-game-asset-handoff.md`
- `references/painter-mcp-playbook.md`

## Screenshot Flow

Capture screenshots at these checkpoints:
- immediately after inspection if the issue is visual
- after rebuilding the base stack
- after the first look-dev pass
- after any major geometry or UV handoff back into Painter
- before export if the user needs sign-off

Use the screenshot workflow in:
- `references/painter-shot-flow.md`

## Reference Map

- PBR reasoning: `references/pbr-guide.md`
- Painter structure and layers: `references/painter-core-workflow.md`
- Material recipes and stylized surface work: `references/painter-material-techniques.md`
- Blender/Painter/export handoff: `references/painter-game-asset-handoff.md`
- MCP-first operating procedure: `references/painter-mcp-playbook.md`
- Screenshot checkpoints and review flow: `references/painter-shot-flow.md`
