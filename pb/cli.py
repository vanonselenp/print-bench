"""
pb — printbench

A small CLI for the prompt → image → 3D model loop, abstracted away
from any specific subject domain. You provide style.md and subjects.yaml;
pb assembles prompts, clipboards them, and pushes chosen images to Meshy.

Commands:
    pb init <project>
    pb prompt <project> <subject>
    pb stage <project> <subject> <variant>
    pb meshify <project> <subject> <variant>
    pb learn <project> "<lesson>"
    pb list <project>
"""

from __future__ import annotations

import os
import re
import sys
import time
import shutil
import base64
import datetime as dt
from pathlib import Path

import click
import yaml
import httpx

try:
    import pyperclip
    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

ROOT = Path(os.environ.get("PB_ROOT", Path.cwd()))
PROJECTS = ROOT / "projects"
TEMPLATES = Path(__file__).parent.parent / "templates"

MESHY_API_BASE = "https://api.meshy.ai/openapi/v1"
MESHY_ENDPOINT = f"{MESHY_API_BASE}/multi-image-to-3d"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def project_dir(project: str) -> Path:
    return PROJECTS / project


def style_path(project: str) -> Path:
    return project_dir(project) / "style.md"


def subjects_path(project: str) -> Path:
    return project_dir(project) / "subjects.yaml"


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
        else:
            if current_name is not None:
                current_body.append(line)

    if current_name is not None:
        sections[current_name] = "\n".join(current_body).strip()

    return sections


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """pb — printbench. Disciplined prompt → image → 3D model loops."""


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
    (target / "seed.png").touch()  # placeholder; user replaces

    click.echo(f"initialised {target}")
    click.echo("next:")
    click.echo(f"  1. edit {target}/style.md")
    click.echo(f"  2. replace {target}/seed.png with your anchor image")
    click.echo(f"  3. add subjects to {target}/subjects.yaml")


@cli.command()
@click.argument("project")
@click.argument("subject")
def prompt(project: str, subject: str) -> None:
    """Assemble a prompt for a subject and copy it to the clipboard.

    Reads style.md, finds the subject's description in subjects.yaml,
    and produces the full brief plus the "suggest 3 variations" trailer.
    Does not call any image API — paste this into ChatGPT yourself,
    after the seed image has set the visual context.
    """
    require_project(project)
    subj = find_subject(project, subject)
    sections = parse_style(project)

    variation_axis = sections.get("Variation axis", "").strip()
    if not variation_axis:
        click.echo("error: style.md has no 'Variation axis' section", err=True)
        sys.exit(1)

    parts = [
        f"# Brief: {project} — {subject}",
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
            click.echo(f"✓ prompt for {project}/{subject} copied to clipboard")
        except pyperclip.PyperclipException:
            click.echo(assembled)
            click.echo("\n(clipboard unavailable — prompt printed above)", err=True)
    else:
        click.echo(assembled)
        click.echo("\n(install pyperclip for clipboard support)", err=True)


@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
def stage(project: str, subject: str, variant: str) -> None:
    """Create a variant folder ready for front.png and back.png."""
    require_project(project)
    find_subject(project, subject)  # validates
    target = project_dir(project) / subject / variant
    target.mkdir(parents=True, exist_ok=True)
    click.echo(f"staged {target}")
    click.echo("drop front.png and back.png in there, then:")
    click.echo(f"  pb meshify {project} {subject} {variant}")


@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
@click.option("--poll-interval", default=10, help="Seconds between status checks.")
@click.option("--timeout", default=900, help="Max seconds to wait for completion.")
def meshify(
    project: str, subject: str, variant: str, poll_interval: int, timeout: int
) -> None:
    """Send chosen front+back images to Meshy and download the STL.

    Requires MESHY_API_KEY in the environment.
    """
    require_project(project)
    find_subject(project, subject)

    variant_dir = project_dir(project) / subject / variant
    front = variant_dir / "front.png"
    back = variant_dir / "back.png"

    for f in (front, back):
        if not f.exists():
            click.echo(f"error: missing {f}", err=True)
            sys.exit(1)

    api_key = os.environ.get("MESHY_API_KEY")
    if not api_key:
        click.echo("error: MESHY_API_KEY not set in environment", err=True)
        sys.exit(1)

    headers = {"Authorization": f"Bearer {api_key}"}

    def to_data_uri(path: Path) -> str:
        b = base64.b64encode(path.read_bytes()).decode()
        return f"data:image/png;base64,{b}"

    payload = {
        "image_urls": [to_data_uri(front), to_data_uri(back)],
        "ai_model": "meshy-5",
        "topology": "triangle",
        "should_remesh": True,
    }

    click.echo(f"submitting {subject}/{variant} to Meshy...")
    with httpx.Client(timeout=60) as client:
        r = client.post(MESHY_ENDPOINT, json=payload, headers=headers)
        if r.status_code >= 300:
            click.echo(f"submit failed: {r.status_code} {r.text}", err=True)
            sys.exit(1)
        task_id = r.json()["result"]

        click.echo(f"task {task_id} created; polling every {poll_interval}s...")
        start = time.time()
        while True:
            if time.time() - start > timeout:
                click.echo("timeout waiting for completion", err=True)
                sys.exit(1)
            time.sleep(poll_interval)
            r = client.get(f"{MESHY_ENDPOINT}/{task_id}", headers=headers)
            r.raise_for_status()
            task = r.json()
            status = task.get("status")
            progress = task.get("progress", 0)
            click.echo(f"  status={status} progress={progress}%")

            if status == "SUCCEEDED":
                stl_url = task.get("model_urls", {}).get("fbx") or task.get(
                    "model_urls", {}
                ).get("glb")
                # Prefer STL if available; fall back to whatever Meshy gives.
                stl_url = task.get("model_urls", {}).get("stl") or stl_url
                if not stl_url:
                    click.echo(
                        f"error: no model URL in response: {task.get('model_urls')}",
                        err=True,
                    )
                    sys.exit(1)
                stl_data = client.get(stl_url).content
                out = variant_dir / "model.stl"
                out.write_bytes(stl_data)
                elapsed = int(time.time() - start)
                click.echo(f"✓ saved {out} ({len(stl_data)} bytes, {elapsed}s)")
                return

            if status in ("FAILED", "CANCELED", "EXPIRED"):
                click.echo(
                    f"task ended in status {status}: {task.get('task_error')}",
                    err=True,
                )
                sys.exit(1)


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
        text = text.replace(
            "<!-- lessons-end -->", f"{entry}\n<!-- lessons-end -->"
        )
    else:
        # Fallback: append under a Lessons heading, creating it if needed.
        if "## Lessons" not in text:
            text += "\n\n## Lessons\n\n"
        text = text.rstrip() + f"\n{entry}\n"

    path.write_text(text)
    click.echo(f"✓ logged: {entry}")


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
        sdir = project_dir(project) / name
        variants = []
        if sdir.is_dir():
            for v in sorted(sdir.iterdir()):
                if not v.is_dir():
                    continue
                has_front = (v / "front.png").exists()
                has_back = (v / "back.png").exists()
                has_stl = (v / "model.stl").exists()
                flag = (
                    "stl"
                    if has_stl
                    else "ready"
                    if has_front and has_back
                    else "partial"
                    if has_front or has_back
                    else "empty"
                )
                variants.append(f"{v.name}[{flag}]")
        rows.append((name, ", ".join(variants) if variants else "—"))

    width = max(len(r[0]) for r in rows)
    for name, vs in rows:
        click.echo(f"  {name.ljust(width)}  {vs}")


if __name__ == "__main__":
    cli()
