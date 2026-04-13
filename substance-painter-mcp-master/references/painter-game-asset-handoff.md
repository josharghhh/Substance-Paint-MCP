# Blender / Painter Game Asset Handoff

Primary Adobe sources:
- Texture Set settings: https://experienceleague.adobe.com/en/docs/substance-3d-painter/using/interface/texture-set/texture-set-settings
- Bake mesh maps: https://experienceleague.adobe.com/en/docs/substance-3d-painter/using/baking/how-to-bake-mesh-maps
- Export settings: https://experienceleague.adobe.com/en/docs/substance-3d-painter/using/getting-started/export/export-window/export-settings

## Ownership Boundaries

Blender owns:
- silhouette
- mesh splits
- material slots
- UV sets
- card density and card geometry
- Geo Nodes generation and baking to mesh

Painter owns:
- texture set authoring
- material response
- mask logic
- map export validation

## Texture Set Truth

Never assume texture-set count.

Always verify:
- mesh names per texture set
- material-slot mapping
- whether the expected separate material still exists after reimport

If a separate fur-card texture set disappears:
- inspect Blender material slots
- inspect export result
- inspect Painter project import result

## UV and Card Problems

If fur cards show opaque shards or random polygon faces:
- check that the opacity texture is sampled on the intended UV set
- check whether the wrong UV set was exported as active/render UV
- check for default opaque layers still enabled in Painter

## Geometry Vs Painter Diagnosis

Fix in Blender when:
- silhouette is wrong
- cards are too long, sparse, or spiky
- cards point in the wrong directions
- material slots or UV sets are wrong

Fix in Painter when:
- color, roughness, normal, or height response is wrong
- the opacity texture choice is wrong
- export packing is wrong

## Export Validation

Before export:
- confirm writable project state
- confirm intended texture sets are present
- confirm preset selection
- confirm output directory and naming

After export:
- verify file count
- verify texture set names in outputs
- verify expected channels landed in the expected maps
