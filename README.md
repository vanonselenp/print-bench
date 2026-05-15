# pb — printbench

A small CLI for the disciplined loop:

    style → subject → 3 pose suggestions (text) → seed image →
    3 visual variations → pick → Meshy → STL → slicer → print

Domain-agnostic. `pb` knows nothing about soldiers, monsters, terrain,
chess pieces, or coat hooks. It knows about a `style.md`, a
`subjects.yaml`, and a Meshy multi-image-to-3D endpoint.

## Install

```bash
pip install -e .
export MESHY_API_KEY=sk-...        # for `pb meshify`
```

Run `pb` from a directory you want to be the root, or set `PB_ROOT`.
Projects live under `$PB_ROOT/projects/`.

## The loop

```bash
pb init soviets
# edit projects/soviets/style.md, drop in seed.png, list subjects in subjects.yaml

pb prompt soviets at-rifle-team
# clipboards the full brief — paste into ChatGPT, get 3 pose suggestions in text,
# then send the seed image and ask for the chosen pose front+back

pb stage soviets at-rifle-team v1
# creates projects/soviets/at-rifle-team/v1/  — drop front.png and back.png there

pb meshify soviets at-rifle-team v1
# uploads both views, polls, downloads model.stl into the same folder

pb learn soviets "Add 'no thin protrusions' — sniper rifle snapped on print bed."
# dated entry appended to style.md
```

## What `pb` deliberately does not do

- **Generate images.** Stays in ChatGPT. The conversational "now show
  me three more" affordance is doing real work in the judgement loop;
  scripting it away would lose the part that matters.
- **Pick the best variation.** That's the user's eye.
- **Slice.** Bambu Studio is fine. Hand off the STL and stop.

## What it does do

- Reuse `style.md` across every subject in a project, so constraints
  learned once are applied every time.
- Make `style.md` a versioned, forkable, shareable artefact with a
  built-in `Lessons` changelog.
- Eliminate the Meshy upload-two-files-and-wait dance.
- Show you at a glance which subjects are sketched, ready, or done.
