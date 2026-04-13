# Painter Core Workflow

Primary Adobe sources:
- Layer stack: https://experienceleague.adobe.com/en/docs/substance-3d-painter/using/interface/layer-stack/layer-stack
- Blending modes: https://experienceleague.adobe.com/en/docs/substance-3d-painter/using/interface/layer-stack/blending-modes
- Masking and effects: https://experienceleague.adobe.com/en/docs/substance-3d-painter/using/interface/layer-stack/masking-and-effects
- Geometry mask: https://experienceleague.adobe.com/en/docs/substance-3d-painter/using/interface/layer-stack/geometry-mask
- Texture Set settings: https://experienceleague.adobe.com/en/docs/substance-3d-painter/using/interface/texture-set/texture-set-settings

## Core Mental Model

- Each texture set is its own material context.
- Layers are multi-channel by default.
- Per-channel opacity and per-channel blending matter.
- Folders are organizational and compositional units, not just UI grouping.

## Preferred Stack Structure

For game assets, prefer explicit role-based layers:
- `base_maps`
- `material_response`
- `breakup`
- `accents`
- `mask drivers`

Avoid:
- unnamed top-level layers
- relying on the default starter `Layer 1`
- hiding critical logic inside unlabelled stacks or mixed-purpose layers

## Fill Layer Rules

Use fill layers when:
- importing source textures
- establishing material defaults
- applying procedural substances
- adding controlled breakup

Use paint layers when:
- hand-painting localized corrections or details
- adding stylized accents that are intentionally brush-driven

## Masking Rules

- Use masks for controlled material separation rather than duplicating whole stacks whenever possible.
- Use grayscale logic for masks, then layer effects procedurally when the separation must remain editable.
- Use geometry masks when broad mesh-based exclusion is needed quickly.

## Texture Set Settings Rules

- Inspect channels before assuming a workflow.
- Confirm mesh maps are present or intentionally absent.
- Confirm that the texture-set resolution and padding match export intent.
- If the normal output looks wrong, check whether the normal channel exists and whether baked maps are still contributing.

## Operational Rules

- Inspect texture sets before importing maps.
- Inspect layer stack before adding more layers.
- If the project reopened from a template, expect only a default paint layer and rebuild cleanly.
