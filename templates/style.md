# Style: {{PROJECT}}

## Intent

One paragraph. What you're making, why, and the visual register in plain
English. Treat this as the brief you'd give a sculptor or illustrator.

e.g. "Chibi WW2 infantry for tabletop play at 2cm scale, in the visual
register of Metal Slug crossed with Fimo clay figures. Single solid
silhouettes that survive 3D printing at small scale."

## Variation axis

The one thing that varies between subjects in this project. This phrase
gets appended verbatim to every prompt as
`Suggest 3 variations for <axis>.`

e.g. `poses only` / `silhouettes only` / `damage states only` /
`costume only, neutral pose` / `facial expressions only`

## Seed reference

`seed.png` — the anchor image. Describe what it locks in:

e.g. "The commissar. Carries the entire visual identity of the army —
proportions, helmet style, the slightly-menacing cartoon register."

## Style constraints

The rules that survive being learned the hard way. Add to this list
whenever a generation goes wrong. These are about how the thing *looks*.

- Chibi proportions, large head ~1/3 body height
- Chunky oversized features, simplified details
- No thin protrusions
- Integrated round base, feet merged to base
- Single solid silhouette, no floating elements

## Print constraints

About the printer and the scale, not the look.

- Target scale: 2cm
- Minimum feature thickness ~0.8mm at this scale
- Weapons and protrusions oversized to survive printing

## View constraints

Rules for the renders, so Meshy gets what it needs.

- Front and back view of the same sculpt, not two sculpts
- Back view is the exact same pose rotated 180°
- Weapon/feature position must match exactly between views
- Silhouette must align when mirrored
- No reinterpretation of pose between views

## Subject template

The shape of a one-line subject description. Filled in per-subject
in `subjects.yaml`.

e.g. `<unit type> (<model count>): <key action>, <visual details>, <distinguishing gear>.`

## Lessons

Append-only. Date-stamped. Use `pb learn` to add entries.

<!-- lessons-start -->
<!-- lessons-end -->
