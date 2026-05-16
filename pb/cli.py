"""
pb - printbench

A small CLI for disciplined prompt -> image -> 3D model loops. The CLI owns
durable project state, local crop artefacts, and upload/fetch boundaries.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import click
import httpx
from PIL import Image
import yaml

try:
    import pyperclip

    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False


ROOT = Path(os.environ.get("PB_ROOT", Path.cwd()))
PROJECTS = ROOT / "projects"
TEMPLATES = Path(__file__).parent.parent / "templates"

MESHY_API_BASE = "https://api.meshy.ai/openapi/v1"
MESHY_ENDPOINT = f"{MESHY_API_BASE}/multi-image-to-3d"
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
VIEW_ORDER = ["front", "back", "left", "right", "top", "bottom"]
TASK_ARCHIVE_RE = re.compile(r"^task\.(\d+)\.json$")


def project_dir(project: str) -> Path:
    return PROJECTS / project


def style_path(project: str) -> Path:
    return project_dir(project) / "style.md"


def subjects_path(project: str) -> Path:
    return project_dir(project) / "subjects.yaml"


def subject_dir(project: str, subject: str) -> Path:
    return project_dir(project) / subject


def variant_dir(project: str, subject: str, variant: str) -> Path:
    return subject_dir(project, subject) / variant


def sources_dir(variant_path: Path) -> Path:
    return variant_path / "sources"


def regions_path(variant_path: Path) -> Path:
    return variant_path / "regions.json"


def task_path(variant_path: Path) -> Path:
    return variant_path / "task.json"


def require_project(project: str) -> None:
    if not project_dir(project).is_dir():
        click.echo(
            f"error: project '{project}' not found at {project_dir(project)}",
            err=True,
        )
        click.echo("hint: pb init " + project, err=True)
        sys.exit(1)


def load_subjects(project: str) -> list[dict]:
    require_project(project)
    path = subjects_path(project)
    if not path.exists():
        return []
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return data.get("subjects") or []


def find_subject(project: str, subject_name: str) -> dict:
    subjects = load_subjects(project)
    for s in subjects:
        if s.get("name") == subject_name:
            return s
    click.echo(f"error: subject '{subject_name}' not found in {project}", err=True)
    click.echo(f"hint: pb list {project}", err=True)
    sys.exit(1)


def parse_style(project: str) -> dict[str, str]:
    """Parse style.md into a dict of section name -> body."""
    text = style_path(project).read_text()
    sections: dict[str, str] = {}
    current_name: str | None = None
    current_body: list[str] = []

    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current_name is not None:
                sections[current_name] = "\n".join(current_body).strip()
            current_name = m.group(1).strip()
            current_body = []
        elif current_name is not None:
            current_body.append(line)

    if current_name is not None:
        sections[current_name] = "\n".join(current_body).strip()

    return sections


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def validate_image(path: Path) -> None:
    if not path.exists():
        click.echo(f"error: image not found: {path}", err=True)
        sys.exit(1)
    if path.suffix.lower() not in IMAGE_EXTS:
        click.echo(f"error: unsupported image format: {path.suffix}", err=True)
        click.echo("hint: use .png, .jpg, or .jpeg", err=True)
        sys.exit(1)


def copy_sources(paths: tuple[str, ...], target: Path) -> list[str]:
    src_dir = sources_dir(target)
    src_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    existing = sorted(
        (
            int(p.stem.split("-", 1)[1])
            for p in src_dir.iterdir()
            if p.is_file() and p.stem.startswith("source-") and p.suffix.lower() in IMAGE_EXTS
        ),
        reverse=True,
    )
    start = existing[0] + 1 if existing else 1

    for idx, raw in enumerate(paths, start=start):
        source = Path(raw).expanduser()
        validate_image(source)
        name = f"source-{idx}{source.suffix.lower()}"
        shutil.copy(source, src_dir / name)
        copied.append(name)
    return copied


def list_sources(target: Path) -> list[str]:
    src_dir = sources_dir(target)
    if not src_dir.is_dir():
        return []
    return sorted(
        p.name for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def crop_regions(target: Path, regions: dict) -> None:
    src_dir = sources_dir(target)
    if not regions:
        raise ValueError("at least one region is required")

    for label, region in regions.items():
        if not re.match(r"^[a-zA-Z0-9_-]+$", label):
            raise ValueError(f"invalid label: {label}")
        source_name = region.get("source")
        box = region.get("box")
        if source_name not in list_sources(target):
            raise ValueError(f"unknown source for {label}: {source_name}")
        if not isinstance(box, list) or len(box) != 4:
            raise ValueError(f"invalid box for {label}")
        x, y, w, h = [int(round(float(v))) for v in box]
        if w <= 0 or h <= 0:
            raise ValueError(f"zero-area box for {label}")

        with Image.open(src_dir / source_name) as img:
            width, height = img.size
            left = max(0, min(x, width))
            upper = max(0, min(y, height))
            right = max(left + 1, min(x + w, width))
            lower = max(upper + 1, min(y + h, height))
            crop = img.crop((left, upper, right, lower))
            crop.save(target / f"{label}.png")

    write_json(regions_path(target), {"regions": regions})


def cropper_html() -> bytes:
    return b"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pb cropper</title>
<style>
body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif; background: #181716; color: #f4efe8; }
header { display: flex; gap: 12px; align-items: center; padding: 12px 16px; background: #25221f; position: sticky; top: 0; z-index: 2; }
select, input, button { font: inherit; padding: 8px; border-radius: 6px; border: 1px solid #5d564e; background: #120f0d; color: #f4efe8; }
button { cursor: pointer; background: #d38331; color: #120f0d; border: 0; font-weight: 700; }
button.secondary { background: #38332e; color: #f4efe8; }
main { display: grid; grid-template-columns: 1fr 260px; gap: 16px; padding: 16px; }
#stage { overflow: auto; background: #0e0d0c; border-radius: 10px; padding: 12px; }
#wrap { position: relative; display: inline-block; }
#img { display: block; max-width: calc(100vw - 340px); height: auto; user-select: none; }
.box { position: absolute; border: 3px solid #ffb000; background: rgb(255 176 0 / 13%); box-sizing: border-box; }
.box span { position: absolute; left: -3px; top: -28px; background: #ffb000; color: #120f0d; padding: 3px 6px; font-weight: 700; border-radius: 4px 4px 0 0; }
aside { background: #25221f; border-radius: 10px; padding: 12px; align-self: start; }
li { margin: 8px 0; }
@media (max-width: 800px) { main { grid-template-columns: 1fr; } #img { max-width: calc(100vw - 56px); } }
</style>
</head>
<body>
<header>
  <label>Source <select id="source"></select></label>
  <label>Label <select id="label"><option>front</option><option>back</option><option>left</option><option>right</option><option>top</option><option>bottom</option><option value="custom">custom</option></select></label>
  <input id="custom" placeholder="custom label" hidden>
  <button id="save">Save crops</button>
  <button class="secondary" id="clear">Clear current label</button>
</header>
<main>
  <section id="stage"><div id="wrap"><img id="img" draggable="false"></div></section>
  <aside><h2>Regions</h2><p>Draw one rectangle per labelled view. Reusing a label replaces its previous box.</p><ul id="regions"></ul></aside>
</main>
<script>
const img = document.getElementById('img');
const wrap = document.getElementById('wrap');
const sourceSelect = document.getElementById('source');
const labelSelect = document.getElementById('label');
const custom = document.getElementById('custom');
const list = document.getElementById('regions');
let state = { sources: [], regions: {} };
let drawing = null;

function label() {
  const raw = labelSelect.value === 'custom' ? custom.value.trim() : labelSelect.value;
  return raw.replace(/[^a-zA-Z0-9_-]/g, '-');
}
function scale() { return img.naturalWidth / img.clientWidth; }
function imagePoint(ev) {
  const r = img.getBoundingClientRect();
  const s = scale();
  return { x: Math.round((ev.clientX - r.left) * s), y: Math.round((ev.clientY - r.top) * s) };
}
function loadSource() { img.src = '/sources/' + encodeURIComponent(sourceSelect.value); render(); }
function render() {
  wrap.querySelectorAll('.box').forEach(e => e.remove());
  const current = sourceSelect.value;
  const s = img.naturalWidth && img.clientWidth ? img.clientWidth / img.naturalWidth : 1;
  for (const [name, region] of Object.entries(state.regions)) {
    if (region.source !== current) continue;
    const [x, y, w, h] = region.box;
    const box = document.createElement('div');
    box.className = 'box';
    box.style.left = (x * s) + 'px';
    box.style.top = (y * s) + 'px';
    box.style.width = (w * s) + 'px';
    box.style.height = (h * s) + 'px';
    box.innerHTML = '<span>' + name + '</span>';
    wrap.appendChild(box);
  }
  list.innerHTML = '';
  for (const [name, region] of Object.entries(state.regions)) {
    const li = document.createElement('li');
    li.textContent = name + ' from ' + region.source + ' [' + region.box.join(', ') + ']';
    list.appendChild(li);
  }
}
async function boot() {
  const res = await fetch('/state');
  state = await res.json();
  for (const src of state.sources) {
    const opt = document.createElement('option'); opt.value = src; opt.textContent = src; sourceSelect.appendChild(opt);
  }
  sourceSelect.addEventListener('change', loadSource);
  labelSelect.addEventListener('change', () => custom.hidden = labelSelect.value !== 'custom');
  img.addEventListener('load', render);
  loadSource();
}
img.addEventListener('mousedown', ev => { if (!label()) return; drawing = { start: imagePoint(ev) }; });
window.addEventListener('mousemove', ev => {
  if (!drawing) return;
  const end = imagePoint(ev); const x = Math.min(drawing.start.x, end.x); const y = Math.min(drawing.start.y, end.y);
  state.regions[label()] = { source: sourceSelect.value, box: [x, y, Math.abs(end.x - drawing.start.x), Math.abs(end.y - drawing.start.y)] };
  render();
});
window.addEventListener('mouseup', () => { drawing = null; });
document.getElementById('clear').addEventListener('click', () => { delete state.regions[label()]; render(); });
document.getElementById('save').addEventListener('click', async () => {
  const res = await fetch('/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ regions: state.regions }) });
  if (!res.ok) { alert(await res.text()); return; }
  document.body.innerHTML = '<main><h1>Saved</h1><p>You can close this tab.</p></main>';
});
boot();
</script>
</body>
</html>"""


def run_cropper(target: Path) -> None:
    shutdown = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send_bytes(200, cropper_html(), "text/html; charset=utf-8")
                return
            if parsed.path == "/state":
                regions = read_json(regions_path(target), {"regions": {}}).get("regions", {})
                body = json.dumps({"sources": list_sources(target), "regions": regions}).encode()
                self.send_bytes(200, body, "application/json")
                return
            if parsed.path.startswith("/sources/"):
                name = unquote(parsed.path.removeprefix("/sources/"))
                if name not in list_sources(target):
                    self.send_bytes(404, b"not found", "text/plain")
                    return
                path = sources_dir(target) / name
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_bytes(200, path.read_bytes(), content_type)
                return
            self.send_bytes(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/save":
                self.send_bytes(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length).decode())
                crop_regions(target, payload.get("regions") or {})
            except Exception as exc:
                self.send_bytes(400, str(exc).encode(), "text/plain")
                return
            self.send_bytes(200, b"ok", "text/plain")
            shutdown.set()

    port = find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    click.echo(f"opening cropper: {url}")
    webbrowser.open(url)
    while not shutdown.is_set():
        server.handle_request()
    server.server_close()


def to_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{encoded}"


def parse_param(value: str):
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def parse_params(params: tuple[str, ...]) -> dict:
    parsed = {}
    for param in params:
        if "=" not in param:
            click.echo(f"error: --param must be k=v, got {param}", err=True)
            sys.exit(1)
        key, value = param.split("=", 1)
        parsed[key] = parse_param(value)
    return parsed


def ordered_views(target: Path) -> list[tuple[str, Path]]:
    regions = read_json(regions_path(target), {"regions": {}}).get("regions", {})
    labels = set(regions.keys())
    labels.update(p.stem for p in target.glob("*.png") if p.stem not in {"source"})
    ordered = [label for label in VIEW_ORDER if label in labels]
    ordered.extend(sorted(labels - set(ordered)))
    return [(label, target / f"{label}.png") for label in ordered if (target / f"{label}.png").exists()]


def submit_meshy(target: Path, params: dict) -> dict:
    api_key = os.environ.get("MESHY_API_KEY")
    if not api_key:
        click.echo("error: MESHY_API_KEY not set in environment", err=True)
        sys.exit(1)

    views = ordered_views(target)
    if not views:
        click.echo(f"error: no cropped view PNGs found in {target}", err=True)
        click.echo("hint: run pb crop first", err=True)
        sys.exit(1)
    if len(views) > 4:
        dropped = ", ".join(label for label, _ in views[4:])
        click.echo(f"warning: Meshy accepts at most 4 images; dropping {dropped}", err=True)
        views = views[:4]

    payload = {
        "image_urls": [to_data_uri(path) for _, path in views],
        "ai_model": "latest",
        "should_texture": False,
        "target_formats": ["stl", "glb"],
    }
    payload.update(params)
    headers = {"Authorization": f"Bearer {api_key}"}

    with httpx.Client(timeout=60) as client:
        response = client.post(MESHY_ENDPOINT, json=payload, headers=headers)
        if response.status_code >= 300:
            click.echo(f"submit failed: {response.status_code} {response.text}", err=True)
            sys.exit(1)
        task_id = response.json()["result"]

    return {
        "backend": "meshy",
        "endpoint": "multi-image-to-3d",
        "task_id": task_id,
        "submitted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "params": params,
        "views": [label for label, _ in views],
    }


def get_meshy_task(task_id: str) -> dict:
    api_key = os.environ.get("MESHY_API_KEY")
    if not api_key:
        click.echo("error: MESHY_API_KEY not set in environment", err=True)
        sys.exit(1)
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=60) as client:
        response = client.get(f"{MESHY_ENDPOINT}/{task_id}", headers=headers)
        if response.status_code >= 300:
            click.echo(f"status failed: {response.status_code} {response.text}", err=True)
            sys.exit(1)
        return response.json()


def download_meshy_model(task: dict) -> bytes:
    urls = task.get("model_urls") or {}
    url = urls.get("stl") or urls.get("glb") or urls.get("fbx")
    if not url:
        click.echo(f"error: no model URL in response: {urls}", err=True)
        sys.exit(1)
    with httpx.Client(timeout=120) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def archive_task(target: Path) -> None:
    current = task_path(target)
    if not current.exists():
        return
    numbers = [
        int(m.group(1))
        for p in target.iterdir()
        if p.is_file() and (m := TASK_ARCHIVE_RE.match(p.name))
    ]
    next_num = max(numbers, default=0) + 1
    current.rename(target / f"task.{next_num}.json")


@click.group()
def cli() -> None:
    """pb - printbench. Disciplined prompt -> image -> 3D model loops."""


@cli.command()
@click.argument("project")
def init(project: str) -> None:
    """Scaffold a new project directory with style.md and subjects.yaml."""
    target = project_dir(project)
    if target.exists():
        click.echo(f"error: {target} already exists", err=True)
        sys.exit(1)

    target.mkdir(parents=True)
    style_src = (TEMPLATES / "style.md").read_text().replace("{{PROJECT}}", project)
    (target / "style.md").write_text(style_src)
    shutil.copy(TEMPLATES / "subjects.yaml", target / "subjects.yaml")
    (target / "seed.png").touch()

    click.echo(f"initialised {target}")
    click.echo("next:")
    click.echo(f"  1. edit {target}/style.md")
    click.echo(f"  2. replace {target}/seed.png with your anchor image")
    click.echo(f"  3. add subjects to {target}/subjects.yaml")


@cli.command()
@click.argument("project")
@click.argument("subject")
def prompt(project: str, subject: str) -> None:
    """Assemble a prompt for a subject and copy it to the clipboard."""
    require_project(project)
    subj = find_subject(project, subject)
    sections = parse_style(project)

    variation_axis = sections.get("Variation axis", "").strip()
    if not variation_axis:
        click.echo("error: style.md has no 'Variation axis' section", err=True)
        sys.exit(1)

    parts = [
        f"# Brief: {project} - {subject}",
        "",
        "## Intent",
        sections.get("Intent", "(missing)"),
        "",
        "## Subject",
        subj["description"].strip(),
        "",
        "## Style constraints",
        sections.get("Style constraints", "(missing)"),
        "",
        "## Print constraints",
        sections.get("Print constraints", "(missing)"),
        "",
        "## View constraints",
        sections.get("View constraints", "(missing)"),
        "",
        f"Suggest 3 variations for {variation_axis}.",
    ]
    assembled = "\n".join(parts)

    if HAS_CLIPBOARD:
        try:
            pyperclip.copy(assembled)
            click.echo(f"prompt for {project}/{subject} copied to clipboard")
        except pyperclip.PyperclipException:
            click.echo(assembled)
            click.echo("\n(clipboard unavailable - prompt printed above)", err=True)
    else:
        click.echo(assembled)
        click.echo("\n(install pyperclip for clipboard support)", err=True)


@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
@click.argument(
    "images",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
)
def crop(project: str, subject: str, variant: str, images: tuple[str, ...]) -> None:
    """Copy source images, then open a browser cropper for labelled views."""
    require_project(project)
    find_subject(project, subject)
    target = variant_dir(project, subject, variant)
    target.mkdir(parents=True, exist_ok=True)
    copied = copy_sources(images, target)
    click.echo(f"added sources: {', '.join(copied)}")
    run_cropper(target)
    click.echo(f"saved crops in {target}")


@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
def recrop(project: str, subject: str, variant: str) -> None:
    """Reopen the cropper for an existing variant without adding sources."""
    require_project(project)
    find_subject(project, subject)
    target = variant_dir(project, subject, variant)
    if not list_sources(target):
        click.echo(f"error: no sources found in {sources_dir(target)}", err=True)
        click.echo(f"hint: pb crop {project} {subject} {variant} <image>", err=True)
        sys.exit(1)
    run_cropper(target)
    click.echo(f"saved crops in {target}")


@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
@click.option("--backend", default="meshy", show_default=True)
@click.option("--param", "params", multiple=True, help="Backend parameter as k=v.")
def upload(project: str, subject: str, variant: str, backend: str, params: tuple[str, ...]) -> None:
    """Submit labelled views to a backend and write task.json. Does not fetch."""
    require_project(project)
    find_subject(project, subject)
    if backend != "meshy":
        click.echo("error: only --backend meshy is implemented", err=True)
        sys.exit(1)
    target = variant_dir(project, subject, variant)
    if not target.is_dir():
        click.echo(f"error: variant not found: {target}", err=True)
        click.echo(f"hint: pb crop {project} {subject} {variant} <image>", err=True)
        sys.exit(1)
    result = submit_meshy(target, parse_params(params))
    write_json(task_path(target), result)
    click.echo(f"submitted {subject}/{variant} to Meshy")
    click.echo(f"task: {result['task_id']}")
    click.echo(f"views: {', '.join(result['views'])}")
    click.echo("next: inspect in Meshy/API, then run pb fetch when worth keeping")


@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
@click.option("--wait", is_flag=True, help="Poll until task completes.")
@click.option("--poll-interval", default=10, show_default=True)
def fetch(project: str, subject: str, variant: str, wait: bool, poll_interval: int) -> None:
    """Fetch model.stl for a completed task. Only downloads when run explicitly."""
    require_project(project)
    find_subject(project, subject)
    target = variant_dir(project, subject, variant)
    task_file = task_path(target)
    if not task_file.exists():
        click.echo(f"error: no task.json in {target}", err=True)
        click.echo(f"hint: pb upload {project} {subject} {variant}", err=True)
        sys.exit(1)
    stored = read_json(task_file, {})
    if stored.get("backend") != "meshy":
        click.echo(f"error: unsupported backend in task.json: {stored.get('backend')}", err=True)
        sys.exit(1)

    while True:
        task = get_meshy_task(stored["task_id"])
        status = task.get("status")
        progress = task.get("progress", 0)
        click.echo(f"status={status} progress={progress}%")
        if task.get("thumbnail_url"):
            click.echo(f"thumbnail: {task['thumbnail_url']}")
        if status == "SUCCEEDED":
            data = download_meshy_model(task)
            out = target / "model.stl"
            out.write_bytes(data)
            stored["fetched_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            stored["model_file"] = "model.stl"
            write_json(task_file, stored)
            click.echo(f"saved {out} ({len(data)} bytes)")
            return
        if status in {"FAILED", "CANCELED"}:
            click.echo(f"task ended in {status}: {task.get('task_error')}", err=True)
            sys.exit(1)
        if not wait:
            click.echo("task not ready; rerun pb fetch later or use --wait", err=True)
            sys.exit(1)
        time.sleep(poll_interval)


@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
@click.option("--backend", default="meshy", show_default=True)
@click.option("--param", "params", multiple=True, help="Backend parameter as k=v.")
def retry(project: str, subject: str, variant: str, backend: str, params: tuple[str, ...]) -> None:
    """Archive the current task.json and resubmit the same cropped views."""
    require_project(project)
    find_subject(project, subject)
    if backend != "meshy":
        click.echo("error: only --backend meshy is implemented", err=True)
        sys.exit(1)
    target = variant_dir(project, subject, variant)
    archive_task(target)
    result = submit_meshy(target, parse_params(params))
    write_json(task_path(target), result)
    click.echo(f"resubmitted {subject}/{variant} to Meshy")
    click.echo(f"task: {result['task_id']}")


@cli.command()
@click.argument("project")
@click.argument("lesson")
def learn(project: str, lesson: str) -> None:
    """Append a dated lesson to style.md."""
    require_project(project)
    path = style_path(project)
    text = path.read_text()

    today = dt.date.today().isoformat()
    entry = f"- {today}: {lesson}"

    if "<!-- lessons-end -->" in text:
        text = text.replace("<!-- lessons-end -->", f"{entry}\n<!-- lessons-end -->")
    else:
        if "## Lessons" not in text:
            text += "\n\n## Lessons\n\n"
        text = text.rstrip() + f"\n{entry}\n"

    path.write_text(text)
    click.echo(f"logged: {entry}")


@cli.command(name="list")
@click.argument("project")
def list_cmd(project: str) -> None:
    """Show subjects and their current state."""
    require_project(project)
    subjects = load_subjects(project)
    if not subjects:
        click.echo(f"no subjects in {project}/subjects.yaml")
        return

    rows = []
    for s in subjects:
        name = s["name"]
        sdir = subject_dir(project, name)
        variants = []
        if sdir.is_dir():
            for v in sorted(sdir.iterdir()):
                if not v.is_dir():
                    continue
                views = ordered_views(v)
                has_task = task_path(v).exists()
                has_stl = (v / "model.stl").exists()
                has_sources = bool(list_sources(v))
                flag = (
                    "stl"
                    if has_stl
                    else "mesh-pending"
                    if has_task
                    else "cropped"
                    if views
                    else "sources"
                    if has_sources
                    else "empty"
                )
                variants.append(f"{v.name}[{flag}]")
        rows.append((name, ", ".join(variants) if variants else "-"))

    width = max(len(r[0]) for r in rows)
    for name, variants in rows:
        click.echo(f"  {name.ljust(width)}  {variants}")


@cli.command()
@click.argument("args", nargs=-1)
def meshify(args: tuple[str, ...]) -> None:
    """Deprecated: use upload then fetch."""
    click.echo("error: meshify was split into upload and fetch", err=True)
    click.echo("hint: pb upload <project> <subject> <variant>; then pb fetch when ready", err=True)
    sys.exit(1)


@cli.command()
@click.argument("args", nargs=-1)
def stage(args: tuple[str, ...]) -> None:
    """Deprecated: crop creates the variant directory."""
    click.echo("error: stage was replaced by crop", err=True)
    click.echo("hint: pb crop <project> <subject> <variant> <image...>", err=True)
    sys.exit(1)


if __name__ == "__main__":
    cli()
