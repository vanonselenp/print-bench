# pb - printbench

A small CLI for the disciplined loop:

    style -> subject -> prompt -> image generation -> labelled crops ->
    Meshy upload -> human judgement -> explicit fetch -> slicer -> print

Domain-agnostic. `pb` knows nothing about soldiers, monsters, terrain,
chess pieces, or coat hooks. It knows about a `style.md`, a
`subjects.yaml`, local image artefacts, and Meshy's multi-image-to-3D API.

## Install

```bash
pip install -e .
export MESHY_API_KEY=sk-...        # for `pb upload` / `pb fetch`
```

Run `pb` from a directory you want to be the root, or set `PB_ROOT`.
Projects live under `$PB_ROOT/projects/`.

## The loop

```bash
pb init soviets
# edit projects/soviets/style.md, drop in seed.png, list subjects in subjects.yaml

pb prompt soviets at-rifle-team
# copies the full brief; paste into ChatGPT/Gemini/Claude and iterate there

pb crop soviets at-rifle-team v1 ~/Downloads/at-team.png
# opens a local browser cropper; draw labelled regions like front/back/top

pb upload soviets at-rifle-team v1 --backend meshy
# uploads cropped views, writes task.json, exits without polling or downloading

pb fetch soviets at-rifle-team v1
# only downloads model.stl once you choose to keep the mesh

pb learn soviets "Tighter front crop helps Meshy resolve the helmet."
# dated entry appended to style.md
```

If the image generator gives separate files instead of one combined sheet,
pass them all to `pb crop`:

```bash
pb crop soviets at-rifle-team v2 ~/Downloads/front.png ~/Downloads/back.png
```

The cropper stores sources and reusable crop state under the variant:

```text
projects/soviets/at-rifle-team/v1/
  sources/source-1.png
  regions.json
  front.png
  back.png
  task.json
  model.stl
```

Use `pb recrop soviets at-rifle-team v1` to reopen the cropper without
adding new sources.

## What `pb` deliberately does not do

- **Generate images.** That stays in the image model UI because the
  conversational "show me three more" loop is judgement work.
- **Pick the best variation.** That's the user's eye.
- **Decide a mesh is good enough.** `pb upload` does not download;
  `pb fetch` is the explicit commit step.
- **Slice.** Bambu Studio or another slicer owns that step.

## What it does do

- Reuse `style.md` across every subject in a project.
- Keep source images, labelled crops, backend task metadata, and final
  models in a browsable project tree.
- Let a single variant use one combined source image or multiple source
  files.
- Submit up to four ordered cropped views to Meshy.
- Show subject/variant state with `pb list`.

## Meshy notes

Meshy's multi-image endpoint accepts one to four images as an ordered array,
not labelled views. `pb` keeps labels locally and uploads them in this order:

```text
front, back, left, right, top, bottom, then any custom labels alphabetically
```

Extra views beyond Meshy's four-image limit are dropped with a warning.
