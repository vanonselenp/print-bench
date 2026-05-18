# pb - printbench

`pb` is a human-in-the-loop workflow for turning consistent AI-generated
concept images into local 3D-printable assets. It automates the mechanical
parts: prompt assembly, file organization, crop extraction, backend upload,
task status, and model retrieval. It deliberately leaves the judgement calls
with the human.

The point is not full automation. The point is removing friction around the
places where taste matters.

## Why This Exists

Making one AI-generated 3D model is easy. Making a coherent collection is
harder.

The friction shows up quickly:

- The shared style prompt gets repeated with one subject line changed.
- Generated images get trapped inside ChatGPT, Gemini, or Claude threads.
- Screenshots, downloads, crop regions, and uploads become manual busywork.
- Backends can produce several plausible results, but deciding whether a mesh is
  good enough is still a human judgement.
- Lessons learned from bad outputs need to feed back into the next prompt.

`pb` treats the filesystem as the durable memory for the workflow. The chat UI
is where image generation and visual judgement happen. The CLI carries state
between those judgement points.

## The Loop

```text
style guide -> subject -> generated images -> human selection ->
labelled crops -> backend upload -> human mesh judgement ->
explicit fetch -> slicer / print
```

Automated:

- Assemble consistent prompts from `style.md` and `subjects.yaml`.
- Store source images, crop regions, cropped views, backend tasks, and models.
- Upload labelled views to a 3D backend.
- Check task status and fetch finished models.
- Open the best local artifact for review.

Human-controlled:

- The style guide.
- The subject list.
- Prompt iteration inside the image model.
- Which generated image to keep.
- Where the front/back/top crop boundaries are.
- Whether the generated mesh is worth keeping.
- What lessons should be recorded.

## Requirements

Required:

- Python 3.10 or newer.
- [`uv`](https://docs.astral.sh/uv/) for Python environment management.
- A terminal or shell.
- A local web browser for the cropper.
- An image-generation tool such as ChatGPT, Gemini, Claude, or another UI that
  can produce concept images.
- A Meshy API key, Hi3D API credentials, or Replicate API token for 3D generation.

Optional but useful:

- A 3D viewer such as macOS Preview, Blender, or Windows 3D Viewer.
- A slicer such as Bambu Studio, OrcaSlicer, Cura, or similar.

Supported 3D backends today:

- Meshy multi-image-to-3D.
- Hi3D image-to-3D / multi-view image-to-3D.
- Replicate predictions, currently mapped for `hyper3d/rodin`.

## Install

Install `uv` first:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then install project dependencies:

```bash
uv sync
uv run pb --help
```

For interactive use, activate the virtualenv so commands and tab completion
feel normal:

```bash
# macOS / Linux
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Alternatively, keep using `uv run`:

```bash
uv run pb list bolt-british
```

## Configure A 3D Backend

### Meshy

Create an API key from Meshy's API settings page:

```text
https://www.meshy.ai/settings/api
```

Set it in your shell before using `pb upload`, `pb status`, or `pb fetch`:

```bash
# macOS / Linux
export MESHY_API_KEY=msy-...
```

```powershell
# Windows PowerShell
$env:MESHY_API_KEY="msy-..."
```

Meshy API-generated assets may not appear in the normal Meshy web gallery. The
reliable API flow is:

```bash
pb upload <subject> <variant>
pb status <subject> <variant>
pb fetch <subject> <variant>
```

Meshy also retains API-generated assets for a limited time on non-enterprise
accounts. Fetch models you want to keep.

### Hi3D

Create API credentials from the Hi3D platform console:

```text
https://platform.hitem3d.ai/console/apiKey
```

Set both values in your shell:

```bash
# macOS / Linux
export HI3D_CLIENT_ID=...
export HI3D_CLIENT_SECRET=...
```

```powershell
# Windows PowerShell
$env:HI3D_CLIENT_ID="..."
$env:HI3D_CLIENT_SECRET="..."
```

Use Hi3D by selecting the backend:

```bash
pb upload hq-lieutenant v1 --backend hi3d
pb status hq-lieutenant v1
pb fetch hq-lieutenant v1
```

Hi3D supports up to four multi-view images in this order: `front`, `back`,
`left`, `right`. Other local crop labels are ignored for Hi3D upload. The
default Hi3D parameters are chosen for printable output: geometry-only,
`hitem3dv2.1`, `1536fast`, and STL format. Override backend parameters with
repeated `--param key=value` flags.

### Replicate

Create an API token from Replicate:

```text
https://replicate.com/account/api-tokens
```

Set it in your shell:

```bash
# macOS / Linux
export REPLICATE_API_TOKEN=r8_...
```

```powershell
# Windows PowerShell
$env:REPLICATE_API_TOKEN="r8_..."
```

Use Replicate by selecting the backend:

```bash
pb upload hq-lieutenant v1 --backend replicate
pb status hq-lieutenant v1
pb fetch hq-lieutenant v1
```

The default Replicate model is `hyper3d/rodin`, using `front`, `back`, `left`,
`right`, and `top` crops when present. The default output request is STL via
`geometry_file_format=stl`, with a generic prompt describing a printable model
from the reference images. Override Replicate inputs with repeated `--param
key=value` flags, including `--param prompt=...` or `--param
model=<owner>/<model>` when you want to try a different Replicate model.

## Create A Project

A project is one coherent style boundary: one army, collection, product line,
terrain set, or visual family.

```bash
pb init bolt-british
cd projects/bolt-british
```

Then edit the project files:

- `style.md`: the reusable visual and print constraints for the whole project.
- `subjects.yaml`: the changing per-model subject descriptions.
- `seed.png`: the reference image that anchors the collection's style.

This separation is the main discipline. The style stays stable across the
collection. The subject changes one model at a time.

## Run One Subject

From inside the project directory:

```bash
pb list
```

Pick a subject slug from `subjects.yaml`, then assemble the prompt:

```bash
pb prompt hq-lieutenant
```

`pb prompt` copies the stable style guide plus the selected subject brief to
the clipboard. Paste it into ChatGPT, Gemini, Claude, or your image tool. This
step stays manual because selecting and refining generated images is judgement
work.

Once you have a generated image worth trying, open the cropper:

```bash
pb crop hq-lieutenant v1
```

Drag the generated image into the browser cropper, or use the upload control.
Draw labelled regions such as `front` and `back`, then save. The cropper stores
the original image, the reusable crop geometry, and the cropped view images.

Submit the cropped views to the default backend, Meshy:

```bash
pb upload hq-lieutenant v1
```

`pb upload` submits the task and exits. It does not poll forever and does not
download the model. This keeps the mesh judgement step explicit.

Check progress:

```bash
pb status hq-lieutenant v1
```

To use Hi3D instead:

```bash
pb upload hq-lieutenant v1 --backend hi3d
```

To use Replicate instead:

```bash
pb upload hq-lieutenant v1 --backend replicate
```

When the result is worth keeping, fetch it:

```bash
pb fetch hq-lieutenant v1
```

Open the best local artifact:

```bash
pb open hq-lieutenant v1
```

Record lessons only when you decide they should change future prompts:

```bash
pb learn "Tighter front crop helps Meshy resolve the helmet."
```

## Context-Aware Commands

`pb` infers project, subject, and variant from the current directory. These are
equivalent:

```bash
pb status bolt-british hq-lieutenant v1
cd projects/bolt-british && pb status hq-lieutenant v1
cd projects/bolt-british/hq-lieutenant && pb status v1
cd projects/bolt-british/hq-lieutenant/v1 && pb status
```

The directory is the working context. There is no hidden `pb use` state to
remember or debug.

From a subject directory, the common loop becomes:

```bash
cd projects/bolt-british/hq-lieutenant

pb prompt
pb crop v1
pb upload v1
pb status v1
pb fetch v1
pb open v1
```

From a variant directory:

```bash
cd projects/bolt-british/hq-lieutenant/v1

pb status
pb fetch
pb open
```

## Cropper Workflow

Run `pb crop` with no image path when Terminal cannot read a folder such as
`~/Downloads`, or when drag-and-drop is more natural:

```bash
pb crop hq-lieutenant v1
```

The browser cropper supports:

- Dragging in one or more PNG/JPEG images.
- Uploading through the file picker.
- Cropping a combined source sheet into labelled views.
- Cropping separate source files into one variant.
- Reopening existing regions with `pb recrop`.

If you already have readable image paths, you can pass them directly:

```bash
pb crop hq-lieutenant v2 ~/Downloads/front.png ~/Downloads/back.png
```

Reopen a variant without adding new sources:

```bash
pb recrop hq-lieutenant v1
```

## Project Layout

The filesystem is the memory layer. A generated image should not live only in a
chat thread.

```text
projects/
  bolt-british/
    style.md
    subjects.yaml
    seed.png
    hq-lieutenant/
      v1/
        sources/
          source-1.png
        regions.json
        front.png
        back.png
        task.json
        model.stl
```

Important files:

- `sources/`: original images from the image-generation tool.
- `regions.json`: labelled crop boxes, so cropping is repeatable.
- `front.png`, `back.png`, etc.: extracted views sent to the selected backend.
- `task.json`: backend task id and upload metadata.
- `model.stl`: downloaded only after `pb fetch` when the backend output is STL.

## Backend Notes

### Meshy

Meshy's multi-image endpoint accepts one to four images as an ordered array,
not labelled views. `pb` keeps labels locally and uploads them in this order:

```text
front, back, left, right, top, bottom, then any custom labels alphabetically
```

Extra views beyond Meshy's four-image limit are dropped with a warning.

Use `pb status` to inspect the API task:

```bash
pb status hq-lieutenant v1
```

It prints the Meshy task status, progress, thumbnail URL when available, and
model URLs when available.

### Hi3D

Hi3D uses a token flow based on `HI3D_CLIENT_ID` and `HI3D_CLIENT_SECRET`.
`pb` obtains a short-lived token for each API interaction.

Hi3D accepts either one image or up to four ordered multi-view images:

```text
front, back, left, right
```

The API returns a temporary model URL when the task succeeds. `pb fetch`
downloads that URL into the variant folder and records the local filename in
`task.json`.

### Replicate

Replicate is a generic prediction API, not one fixed 3D API. `pb` creates an
async prediction, stores the prediction id in `task.json`, polls it through
`pb status`, and downloads the selected file output through `pb fetch`.

The first supported mapping targets `hyper3d/rodin`, because it accepts
multi-view image inputs and can return STL. `pb` sends data URI inputs from the
local crop PNGs. Replicate recommends hosted files for larger inputs, so very
large crops may need resizing or a future hosted-file upload path.

Replicate API prediction input and output data is retained for a limited time by
default. Fetch outputs you want to keep.

## Troubleshooting

`pb: command not found`

Activate the virtualenv or prefix commands with `uv run`:

```bash
source .venv/bin/activate
# or
uv run pb --help
```

`MESHY_API_KEY not set`

Set the API key in your shell:

```bash
export MESHY_API_KEY=msy-...
```

On Windows PowerShell:

```powershell
$env:MESHY_API_KEY="msy-..."
```

`HI3D_CLIENT_ID and HI3D_CLIENT_SECRET must be set`

Create credentials in the Hi3D platform console, then set both variables:

```bash
export HI3D_CLIENT_ID=...
export HI3D_CLIENT_SECRET=...
```

`REPLICATE_API_TOKEN not set`

Create a token from Replicate, then set it in your shell:

```bash
export REPLICATE_API_TOKEN=r8_...
```

Model exists over the API but not on the Meshy site

Meshy API tasks may not appear in the normal web gallery. Use `pb status` and
`pb fetch`; fetch anything you want to retain locally.

macOS cannot read `~/Downloads`

Use browser drag-and-drop instead:

```bash
pb crop hq-lieutenant v1
```

Then drag the image into the cropper page. The browser file picker has explicit
permission to read the selected file.

Task is still pending

Check later:

```bash
pb status hq-lieutenant v1
```

Or wait until done:

```bash
pb fetch hq-lieutenant v1 --wait
```

No `model.stl`

The model is only downloaded after `pb fetch`. `pb upload` intentionally does
not download anything. Hi3D and Replicate can return other formats if you
override backend parameters; `pb open` uses the fetched filename recorded in
`task.json`.

## Command Reference

| Command | Purpose |
|---|---|
| `pb init <project>` | Create `style.md`, `subjects.yaml`, and `seed.png`. |
| `pb list [project]` | Show subjects and variant state. |
| `pb prompt [project] [subject]` | Copy the assembled prompt to the clipboard. |
| `pb crop [project] [subject] <variant>` | Open the browser cropper and add source images. |
| `pb recrop [project] [subject] <variant>` | Reopen the cropper for existing sources. |
| `pb upload [project] [subject] <variant>` | Submit cropped views to the selected backend and write `task.json`. |
| `pb status [project] [subject] <variant>` | Print backend task status and URLs. |
| `pb fetch [project] [subject] <variant>` | Download the backend model when the task is complete. |
| `pb retry [project] [subject] <variant>` | Archive the previous task and resubmit the same crops. |
| `pb open [project] [subject] <variant>` | Open the best local artifact for review. |
| `pb learn [project] "<lesson>"` | Append a dated lesson to `style.md`. |

Arguments shown in brackets can usually be inferred from the current directory.
