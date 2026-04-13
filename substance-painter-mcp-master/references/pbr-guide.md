# Adobe-First PBR Guide

Primary Adobe sources:
- Adobe Substance 3D PBR overview: https://www.adobe.com/au/products/substance3d/discover/pbr.html
- Adobe Learn, The PBR Guide Part 2: https://www.adobe.com/us/learn/substance-3d-designer/web/the-pbr-guide-part-2

## What PBR Means In Practice

Use PBR as a discipline, not just a texture naming scheme.

- Base color describes the surface color response, not baked-in lighting.
- Roughness controls the spread of reflections and is usually the strongest lever for tactile feel.
- Metallic is usually binary or near-binary for most game assets.
- Normal and height add surface structure, but should not replace silhouette changes.
- Specular behavior still matters even in metal/roughness workflows because Fresnel and material response remain physically constrained.

## Practical Guardrails

- Prefer roughness changes before normal changes when trying to make a surface feel soft, dusty, chalky, or plush.
- Keep metallic at or near zero for felt, fabric, plush, skin, wood, and painted nonmetal surfaces.
- Treat height as subtle in Painter unless the target workflow explicitly needs aggressive depth.
- Avoid noisy base color as a substitute for proper material response.
- Preserve value and color readability first; add breakup second.

## Artistic Read Rules

- If a material feels wrong but the silhouette is right, start with roughness.
- If the asset looks crunchy or spiky, reduce normal and height before repainting color.
- If the asset still reads as the wrong object class, the problem may be silhouette or geometry, not the texture.

## Painter-Specific PBR Translation

- Use fill layers to establish physically plausible defaults quickly.
- Tune per-channel opacity instead of relying on one layer to do everything.
- Build explicit layers for:
  - base truth
  - material response
  - breakup
  - masks or accents

## Common Failure Modes

- Overbright base color that kills form
- Metallic contamination on nonmetal materials
- Using normal noise where roughness variation should do the job
- Using Painter height to fake geometry that should be modeled in Blender
