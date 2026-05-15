# printbench redesign — multi-backend mesh dispatch + region cropper

Date: 2026-05-15
Status: Spec — awaiting user review

## Context

`pb` (printbench) is a CLI for the disciplined loop:

    style → subject → 3 pose suggestions (text) → seed image →
    chosen variation (front+back images) → Meshy → STL → slicer → print

It works today, but produces friction at the scale of a whole collection (20+ subjects sharing one cohesive style). The user has identified three pain points:

1. **Images escape ChatGPT.** Once generated, chosen images live inside a ChatGPT thread. Across many subjects the collection is unbrowsable; images get lost. The user wants every chosen image landing in a structured local tree per project/subject/variant.
2. **Backend coupling.** `pb meshify` is hard-wired to Meshy. The user wants to dispatch the same labelled images to Meshy, Hi3D, or any other multi-image-to-3D backend without code changes to the call site.
3. **Flattened judgement at the mesh layer.** Current `meshify` submits, polls, auto-downloads. The user has explicitly corrected this: there is taste and judgement at the Meshy layer too — the user wants to look at the produced mesh, possibly regenerate with different params, and only commit (download) when satisfied.

The through-line: **preserve every judgement step, let the tooling carry the boring parts between them.** This generalises the existing README rule ("picking the best variation is the user's eye") to every stage that looks mechanical but has taste in it.

A separate question — "should this be packaged as a Claude Code skill?" — is addressed below as a Phase 2 concern. Phase 1 is the CLI refactor + cropper.

## Non-goals

- Replacing the conversational image-generation step inside ChatGPT/Gemini/Claude. The "show me three more" affordance is doing real judgement work and is preserved.
- Picking the best variation. User's eye.
- Choosing when a mesh is good enough to commit. User's eye.
- Slicing. Bambu Studio is fine; hand off the STL and stop.
- A full GUI app wrapping the whole loop. The cropper is the one mouse-driven moment; everything else stays in the terminal.

## Architecture

Three components, ordered by load-bearing-ness:

1. **`pb` CLI (refactored).** Durable foundation. Owns project state, prompt assembly, the localhost cropper server, mesh-backend dispatch, and the lessons log.
2. **Localhost cropper.** Tiny single-page web UI served by `pb crop`. Exists because mouse-driven labelled-region selection is the right tool for that one moment. Python stdlib `http.server` + one HTML file with vanilla JS. No framework.
3. **Claude Code skill `printbench` (Phase 2).** Conversational front-end over the CLI. Built only after Phase 1 ships and is used in anger for at least one project. Out of scope for this spec beyond the brief description below.

```
                  ┌─────────────────────────────┐
                  │   Claude Code skill         │  ← Phase 2
                  │   "printbench"              │     (optional)
                  └──────────────┬──────────────┘
                                 │ shells out to
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │                pb CLI (refactored)                  │
   │   - project state (style.md, subjects.yaml)         │
   │   - prompt assembly                                 │
   │   - localhost cropper server                        │
   │   - mesh backend dispatch (Meshy, Hi3D, ...)        │
   │   - lessons log                                     │
   └───────┬──────────────────┬─────────────────┬────────┘
           │ serves           │ POSTs to        │ HTTP
           ▼                  ▼                 ▼
    localhost:PORT       adapters/          backends
    cropper (HTML+JS)    meshy.py, hi3d.py  (Meshy API, Hi3D API)
```

## Project tree (per variant)

A *project* is one cohesive style. A *subject* is one thing to print. A *variant* is one attempt at one subject. New variant whenever you start over from a fresh generation; old variants are untouched and comparable.

```
projects/
  soviets/                      # project (one cohesive style)
    style.md                    # shared across every subject
    subjects.yaml
    seed.png

    at-rifle-team/              # subject
      v1/                       # variant
        source.png              # raw chosen image from image gen
        regions.json            # cropper's source of truth: {front: [x,y,w,h], ...}
        front.png               # crops produced from source + regions
        back.png
        top.png                 # arbitrary number of labelled views
        task.json               # {backend, task_id, preview_url, submitted_at, params}
        model.stl               # only present after `pb fetch` — never auto-downloaded
      v2/
        ...

    commissar/
      v1/
        ...
```

Rationale:
- `source.png` + `regions.json` are first-class artefacts (user-confirmed). Re-cropping is cheap and replayable; if a lesson teaches you a tighter front crop helps the backend, you re-open the cropper on the same source without regenerating the image.
- `task.json` separate from `model.stl` is the encoded "judgement at the mesh layer" rule — a task can be submitted, have a preview URL waiting indefinitely, and never produce a `model.stl` if the user decides the mesh is not good enough.
- N labelled views (not hardcoded front/back) — backends that accept 4 views (Meshy) and single-view backends are both addressable from the same project tree; the adapter takes what it can use.

## Command surface

| Command | Purpose | Change vs today |
|---|---|---|
| `pb init <project>` | Scaffold project dir with `style.md`, `subjects.yaml`, placeholder `seed.png` | unchanged |
| `pb prompt <project> <subject>` | Assemble brief from style.md + subject, clipboard it | unchanged |
| `pb crop <project> <subject> <variant> <image>` | Auto-create variant dir, copy image → `source.png`, open localhost cropper, on save write `regions.json` + per-view PNGs | **new (subsumes `stage`)** |
| `pb upload <project> <subject> <variant> [--backend X] [--param k=v ...]` | Submit labelled views to backend, write `task.json` with task id + preview URL, exit | **was `meshify`, now upload-only** |
| `pb fetch <project> <subject> <variant>` | Read `task.json`, check status, download model → `model.stl`. Only when user runs it. | **new (extracted from `meshify`)** |
| `pb retry <project> <subject> <variant> [--backend X] [--param k=v ...]` | Resubmit same crops with different backend/params; archive prior `task.json` → `task.N.json` where N is the next free integer | **new** |
| `pb learn <project> "<lesson>"` | Append dated entry to style.md | unchanged |
| `pb list <project>` | Per-subject summary with variant states: `empty` / `cropped` / `mesh-pending` / `mesh-ready` / `stl` | **extended state vocabulary** |

`pb stage` is removed. `pb crop` auto-creates the variant directory; "reserving" an empty variant has no real use case.

`pb upload` blocks only for the submit round trip (seconds), then exits — it never polls for completion. This is the load-bearing change versus today's `meshify`. The user evaluates the preview URL out-of-band.

## Variant lifecycle

```
pb init soviets
# edit style.md, drop seed.png, add subjects to subjects.yaml

pb prompt soviets at-rifle-team
# brief is on clipboard — paste into ChatGPT/Gemini/Claude
# iterate conversationally, save the chosen final image to ~/Downloads/

pb crop soviets at-rifle-team v1 ~/Downloads/at-team.png
# opens http://localhost:PORT in browser
# draw box → label "front", another box → "back", optional "top"
# click save — server exits, files land in variant dir

pb upload soviets at-rifle-team v1 --backend meshy
# prints: task abc123 submitted; preview: https://meshy.ai/...
# user opens preview URL, looks at mesh, decides

pb fetch soviets at-rifle-team v1
# downloads model.stl into variant dir
# OR: pb retry soviets at-rifle-team v1 --param topology=quad

pb learn soviets "Tighter front crop helps Meshy resolve the helmet."

pb list soviets
# at-rifle-team    v1[stl]
# commissar        v1[mesh-pending]  v2[cropped]
# rifle-squad      —
```

Every command is idempotent and safe to re-run. Re-running `pb crop` reopens the cropper with the existing `regions.json` boxes pre-drawn — you nudge, you don't redraw.

## Backend adapter contract

Each backend is one Python file in `pb/backends/`, implementing this protocol:

```python
class MeshBackend(Protocol):
    name: str                    # "meshy", "hi3d", ...
    env_key: str                 # "MESHY_API_KEY", "HI3D_API_KEY"
    accepted_views: set[str]     # e.g. {"front", "back", "left", "right"}

    def submit(self, views: dict[str, Path], params: dict) -> SubmitResult: ...
    # → {"task_id": str, "preview_url": str}

    def status(self, task_id: str) -> StatusResult: ...
    # → {"state": "pending" | "ready" | "failed",
    #    "model_urls": {"stl": ..., "glb": ...} | None,
    #    "error": str | None}

    def fetch(self, task_id: str) -> bytes: ...
    # → raw bytes for the chosen model file (STL preferred, fall back to GLB/FBX)
```

- `views`: `{"front": Path, "back": Path, ...}` — adapter takes only labels in its `accepted_views`, logs and drops the rest.
- `params`: free dict; backend-specific knobs (`topology=triangle`, `should_remesh=true`, Hi3D equivalents) live in the adapter without polluting the CLI surface. `pb upload --param k=v` repeated for each. Values arrive at the adapter as strings; the adapter is responsible for type coercion (`"true"` → `True`, etc.) and for rejecting unknown keys with a clear error.
- `pb upload <project> <subject> <variant> --backend X` looks up the adapter by name, calls `submit`, writes `task.json`.
- `pb fetch` reads `task.json`, looks up the adapter, calls `status`; if `ready`, calls `fetch` and writes `model.stl`. If `pending`, prints status and exits non-zero — caller can re-run later or use `--wait` to poll.
- New backend = one new file in `pb/backends/` + one line in a registry dict. No core changes.
- The existing Meshy code in `pb/cli.py` is extracted to `pb/backends/meshy.py`, edited to fit the protocol.

API keys: env vars per backend (`MESHY_API_KEY`, `HI3D_API_KEY`, …) read by the adapter. The CLI does not hold credentials.

## Cropper internals

- Python stdlib `http.server` running on a chosen free port (picked at runtime to avoid collisions).
- Three routes:
  - `GET /` → HTML page (one vanilla-JS file, ~200 lines, no framework). Loads `regions.json` if present so previously-drawn rectangles are restored.
  - `GET /source.png` → serves the variant's source image.
  - `POST /save` → receives `{regions: {<label>: [x, y, w, h], ...}}`, runs PIL crops, writes per-view PNGs + updated `regions.json`, then returns 200 and signals the CLI to shut the server down and exit cleanly.
- Re-running `pb crop` on a variant with existing `regions.json` restores the rectangles for nudging.
- Labels: a dropdown of common ones (`front`, `back`, `left`, `right`, `top`) plus a "custom" text option. The cropper does not enforce which labels are valid — the backend adapter handles "I can't use this view, dropping it."
- New dependency: `pillow>=10` (for cropping). Stdlib `http.server` covers the rest.

## Error handling

- Missing project / subject / variant / source image: clear error + a hint command (existing pattern in `cli.py`).
- Cropper port in use: pick a free port. Print the chosen URL.
- Cropper save with zero-area or overlapping-to-meaninglessness regions: reject with a message; the cropper UI also disables save until at least one labelled region exists.
- `pb upload` submit failure: print backend error, exit non-zero. No state written to `task.json`.
- `pb fetch` while task is still pending: print state + preview URL, exit non-zero. User re-runs later, or uses `--wait`.
- `pb fetch` on a `failed` task: print error, exit non-zero. User can run `pb retry` with different params.
- Missing backend API key env var: print the expected env var name and exit before any submission.

## Testing

- **pb core**: unit tests for `parse_style`, `find_subject`, project-dir resolution, command argument validation. Light, mirrors existing code style.
- **Backend adapters**: mock HTTP, unit-test `submit` / `status` / `fetch` for each. One opt-in integration test per backend gated behind `RUN_BACKEND_INTEGRATION=1` and the relevant API key.
- **Cropper**: POST a known regions JSON to `/save` against a known source image, assert files saved at expected paths with expected pixel dimensions. Do not automate browser-side mouse interactions.
- **Skill**: no automated tests; validation is "does it feel right when used."

## Phase 2 — Claude Code skill `printbench`

Out of scope for this spec beyond the sketch below. Built after Phase 1 ships and is used in anger for at least one project, so the skill is shaped by real usage.

Likely capabilities:
- Reads state via `pb list <project>` to answer "what's next?"
- When the user attaches a generated image to the chat, uses vision to propose initial bounding boxes for each labelled view, then invokes `pb crop --prefill` (a new flag, added in Phase 2) that opens the cropper with those rectangles pre-drawn. User nudges rather than starts blank.
- Surfaces lesson suggestions after a bad mesh ("rifle barrel measured ~0.5mm at print scale — run `pb learn` with 'no thin protrusions on weapon barrels'?"). Never logs unilaterally.
- Never makes a judgement call — only proposes; user confirms.

Explicitly out of scope for the skill: picking variations, choosing meshes, deciding when to commit. Those remain the user's.

## Migration plan

1. Extract the existing Meshy code in `pb/cli.py` into `pb/backends/meshy.py`, fitting the adapter protocol. `pb meshify` is removed outright, not preserved as an alias — keeping the auto-download behaviour would contradict the judgement-preservation rule. Running the old command should print a one-line "renamed: use `pb upload` then `pb fetch` — see README" and exit non-zero.
2. Add `pb crop`, `pb upload`, `pb fetch`, `pb retry`.
3. Remove `pb stage` (it's subsumed by `pb crop`'s auto-create).
4. Extend `pb list` state vocabulary.
5. Add a second backend stub (`pb/backends/hi3d.py`) to validate the adapter shape against more than just Meshy, even if Hi3D support is initially a placeholder.

## Open questions

None at spec time. Resolved during brainstorming:
- ✅ Region-crop-on-single-image is the right model for image capture
- ✅ `source.png` + `regions.json` are first-class artefacts in the variant dir
- ✅ `stage` is subsumed by `crop`
- ✅ Adapter shape: `submit` / `status` / `fetch` + free-form params dict
- ✅ Cropper stack: stdlib `http.server` + vanilla JS
- ✅ Skill is Phase 2, designed against real Phase-1 usage
