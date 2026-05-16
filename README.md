# pb - printbench

A small CLI for the disciplined loop:

    style -> subject -> prompt -> image generation -> labelled crops ->
    Meshy upload -> human judgement -> explicit fetch -> slicer -> print

Domain-agnostic. `pb` knows nothing about soldiers, monsters, terrain,
chess pieces, or coat hooks. It knows about a `style.md`, a
`subjects.yaml`, local image artefacts, and Meshy's multi-image-to-3D API.

## Install

```bash
uv sync
export MESHY_API_KEY=sk-...        # for `pb upload` / `pb fetch`
```

Run commands with `uv run pb` from a directory you want to be the root, or
set `PB_ROOT` and run from the repository. Projects live under
`$PB_ROOT/projects/`.

For convenience, the examples below use `pb`. If you have not activated the
virtualenv, prefix commands with `uv run`.

```bash
uv run pb --help
```

For interactive use, activate the virtualenv or add an alias so tab
completion and paths feel normal:

```bash
source .venv/bin/activate
# or: alias pb='uv run pb'
```

## The loop

```bash
pb init soviets
# edit projects/soviets/style.md, drop in seed.png, list subjects in subjects.yaml

cd projects/soviets

pb list

pb prompt at-rifle-team
# copies the full brief; paste into ChatGPT/Gemini/Claude and iterate there

pb crop at-rifle-team v1
# opens a local browser cropper; draw labelled regions like front/back/top
# drag an image into the browser, or use the upload control

pb upload at-rifle-team v1 --backend meshy
# uploads cropped views, writes task.json, exits without polling or downloading

pb status at-rifle-team v1
# prints Meshy task progress, thumbnail URL, and model URLs when available

pb fetch at-rifle-team v1
# only downloads model.stl once you choose to keep the mesh

pb open at-rifle-team v1
# opens model.stl, front.png, sources/, or the variant folder; whichever exists first

pb learn "Tighter front crop helps Meshy resolve the helmet."
# dated entry appended to style.md
```

Commands infer context from the current directory. These are equivalent:

```bash
pb status soviets at-rifle-team v1
cd projects/soviets && pb status at-rifle-team v1
cd projects/soviets/at-rifle-team && pb status v1
cd projects/soviets/at-rifle-team/v1 && pb status
```

If the image generator gives separate files instead of one combined sheet,
pass them all to `pb crop`:

```bash
pb crop at-rifle-team v2 ~/Downloads/front.png ~/Downloads/back.png
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

Use `pb recrop at-rifle-team v1` to reopen the cropper without adding new
sources.

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
- Infer project, subject, and variant from the current directory so common
  commands stay short.

## Meshy notes

Meshy's multi-image endpoint accepts one to four images as an ordered array,
not labelled views. `pb` keeps labels locally and uploads them in this order:

```text
front, back, left, right, top, bottom, then any custom labels alphabetically
```

Extra views beyond Meshy's four-image limit are dropped with a warning.
