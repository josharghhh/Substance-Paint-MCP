# Painter Material Techniques

Primary Adobe source family:
- Painter layer stack, masking, and texture set docs on Experience League

## Felt / Plush

Use for stylized toys, plush creatures, and soft fabric-coated assets.

Approach:
- preserve original base color identity through a base map layer
- add a felt or plush material layer above it
- keep felt base-color contribution low unless recoloring is intended
- drive the tactile read mostly through roughness
- add very light normal and height only after roughness is correct

Defaults:
- metallic near zero
- roughness high
- specular low
- color variation restrained
- normal subtle

If it reads wrong:
- too plastic: increase roughness, lower specular
- too noisy: reduce normal and height
- too washed out: reduce felt base-color opacity

## Painted / Worn Surfaces

- start from a clean painted base
- add edge wear and dirt through masks, not random overall noise
- keep macro breakup separate from micro breakup

## Stylized Creatures

- preserve face readability first
- keep cheeks, eyes, and beak/feet accents stable while changing surface feel
- do not bury expression under procedural noise

## Fur Cards In Painter

Painter can tune fur-card response, but it does not fix bad fur-card geometry.

Painter is good for:
- color treatment
- roughness response
- subtle normal/height shaping
- opacity texture selection and review

Painter is not the right place for:
- card density problems
- card silhouette problems
- bad card placement or orientation
- material-slot or UV topology mistakes

## “Less Is More” Rule

For stylized soft materials:
- one clean base layer
- one material-response layer
- one breakup layer

is usually better than many overlapping noisy layers.
