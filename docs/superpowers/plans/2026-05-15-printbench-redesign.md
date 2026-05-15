# printbench redesign — Phase 1 implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `pb` to split mesh dispatch into `upload`/`fetch` (preserve judgement at the mesh layer), add a localhost region cropper for labelled multi-view capture from a single generated image, and introduce a pluggable backend adapter protocol so the same project can dispatch to Meshy, Hi3D, or future backends.

**Architecture:** Three modules in `pb/`: a thin `cli.py` of Click commands; a `state.py` for project/subject/variant path & style helpers; a `backends/` package implementing a `MeshBackend` Protocol with one adapter per provider. A `cropper.py` module serves a tiny stdlib-`http.server` UI that writes labelled crops via PIL. State per variant is captured on disk in `source.png` + `regions.json` + per-view PNGs + `task.json` + `model.stl`.

**Tech Stack:** Python 3.10+, Click, httpx, PyYAML, pyperclip (existing); add Pillow (PIL) for cropping and pytest (+ pytest-mock) for tests. Stdlib `http.server` for the cropper. No new web framework.

---

## Reference: shared types and conventions

Tasks below produce code that interlocks. To prevent drift, these are the canonical names, signatures, and shapes every task must match.

**Project layout (per variant):**

```
projects/<project>/<subject>/<variant>/
  source.png       # raw chosen image from image gen
  regions.json     # {"front": [x, y, w, h], "back": [...], ...}  (ints, pixels)
  front.png        # cropped views derived from source + regions
  back.png
  <label>.png      # arbitrary labels allowed
  task.json        # see schema below; absent until first `pb upload`
  task.1.json      # archived prior task.json after a `pb retry`
  task.2.json      # next archive
  model.stl        # absent until `pb fetch` succeeds
```

**`task.json` schema:**

```json
{
  "backend": "meshy",
  "task_id": "abc123",
  "preview_url": "https://meshy.ai/...",
  "submitted_at": "2026-05-15T17:30:00Z",
  "params": {"topology": "triangle", "should_remesh": "true"},
  "views_used": ["front", "back"]
}
```

`params` values are stored as strings (the form they arrive in from `--param k=v`). Adapters coerce types.

**`MeshBackend` protocol (defined in Task 4):**

```python
from typing import Protocol, TypedDict
from pathlib import Path

class SubmitResult(TypedDict):
    task_id: str
    preview_url: str

class StatusResult(TypedDict):
    state: str  # "pending" | "ready" | "failed"
    model_urls: dict[str, str] | None  # e.g. {"stl": "...", "glb": "..."}
    error: str | None

class MeshBackend(Protocol):
    name: str
    env_key: str
    accepted_views: set[str]

    def submit(self, views: dict[str, Path], params: dict[str, str]) -> SubmitResult: ...
    def status(self, task_id: str) -> StatusResult: ...
    def fetch(self, task_id: str, model_urls: dict[str, str]) -> bytes: ...
```

`fetch` takes the model_urls returned by a prior `status` call, so the adapter doesn't need to re-query before downloading.

**Variant state vocabulary (used by `pb list`):**

| State | Condition |
|---|---|
| `empty` | variant dir exists, no `source.png` |
| `cropped` | `source.png` and at least one `<label>.png` exist, no `task.json` |
| `mesh-pending` | `task.json` exists, no `model.stl` |
| `mesh-ready` | `task.json` exists, last status check returned `ready`, no `model.stl` |
| `stl` | `model.stl` exists |

`pb list` does not call backends. It infers state purely from on-disk files. `mesh-ready` vs `mesh-pending` is distinguished only after a `pb fetch` has been attempted (which writes the resolved status into `task.json` as a `last_status` field — added in Task 12).

---

### Task 1: Set up test infrastructure & dev deps

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dev deps and PIL to pyproject.toml**

Edit `pyproject.toml` to add `pillow` to runtime deps and a `dev` optional-dependencies group:

```toml
[project]
name = "pb-printbench"
version = "0.1.0"
description = "Disciplined prompt → image → 3D model loops, domain-agnostic."
requires-python = ">=3.10"
dependencies = [
    "click>=8.1",
    "httpx>=0.27",
    "PyYAML>=6.0",
    "pyperclip>=1.8",
    "pillow>=10",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-mock>=3.12"]

[project.scripts]
pb = "pb.cli:cli"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["pb*"]

[tool.setuptools.package-data]
pb = ["cropper_assets/*.html"]
```

- [ ] **Step 2: Install with dev extras**

Run: `pip install -e ".[dev]"`
Expected: install completes, `pytest` and `PIL` available.

- [ ] **Step 3: Create tests package and shared fixtures**

Create `tests/__init__.py` (empty file).

Create `tests/conftest.py`:

```python
"""Shared pytest fixtures for pb tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def pb_root(tmp_path, monkeypatch):
    """Set PB_ROOT to a tmp dir and reload pb to pick it up."""
    monkeypatch.setenv("PB_ROOT", str(tmp_path))
    # Force pb modules to re-evaluate ROOT each call.
    import importlib
    import pb.state
    importlib.reload(pb.state)
    return tmp_path


@pytest.fixture
def project(pb_root):
    """A bare-bones project with style.md, subjects.yaml, seed.png."""
    proj = pb_root / "projects" / "soviets"
    proj.mkdir(parents=True)
    (proj / "style.md").write_text(
        "# Style: soviets\n\n"
        "## Intent\nTest project.\n\n"
        "## Variation axis\nposes only\n\n"
        "## Style constraints\n- chunky\n\n"
        "## Print constraints\n- 2cm\n\n"
        "## View constraints\n- front and back\n\n"
        "## Lessons\n\n<!-- lessons-start -->\n<!-- lessons-end -->\n"
    )
    (proj / "subjects.yaml").write_text(
        "subjects:\n"
        "  - name: at-rifle-team\n"
        "    description: |\n"
        "      AT rifle team (2 models): test description.\n"
    )
    (proj / "seed.png").write_bytes(_one_pixel_png())
    return proj


@pytest.fixture
def variant(project):
    """A staged variant dir with a non-trivial source.png ready for cropping."""
    v = project / "at-rifle-team" / "v1"
    v.mkdir(parents=True)
    (v / "source.png").write_bytes(_solid_png(width=200, height=100))
    return v


def _one_pixel_png() -> bytes:
    """Smallest valid PNG (1x1 transparent)."""
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )


def _solid_png(width: int, height: int) -> bytes:
    """A solid-color PNG of the given size — used for crop tests."""
    from PIL import Image
    import io
    img = Image.new("RGB", (width, height), color=(128, 64, 192))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
```

- [ ] **Step 4: Verify pytest discovers the empty suite**

Run: `pytest -q`
Expected: `no tests ran` (or `0 passed`) — no errors.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/__init__.py tests/conftest.py
git commit -m "test: add pytest infrastructure, pillow dep, shared fixtures"
```

---

### Task 2: Extract state helpers to `pb/state.py`

**Files:**
- Create: `pb/state.py`
- Modify: `pb/cli.py` (remove helpers, add imports)
- Test: `tests/test_state.py`

This is a pure refactor — no behavioural change. The goal is to slim `cli.py` so subsequent tasks can add commands without bloating it.

- [ ] **Step 1: Write failing tests for state helpers**

Create `tests/test_state.py`:

```python
"""Tests for pb.state — project/subject path and style parsing helpers."""

from __future__ import annotations

import pytest

from pb import state


def test_project_dir(project):
    assert state.project_dir("soviets") == project


def test_style_path(project):
    assert state.style_path("soviets") == project / "style.md"


def test_subjects_path(project):
    assert state.subjects_path("soviets") == project / "subjects.yaml"


def test_load_subjects_returns_list(project):
    subjects = state.load_subjects("soviets")
    assert len(subjects) == 1
    assert subjects[0]["name"] == "at-rifle-team"


def test_find_subject_returns_dict(project):
    s = state.find_subject("soviets", "at-rifle-team")
    assert s["name"] == "at-rifle-team"


def test_find_subject_missing_exits(project):
    with pytest.raises(SystemExit):
        state.find_subject("soviets", "nope")


def test_parse_style_returns_sections(project):
    sections = state.parse_style("soviets")
    assert "Intent" in sections
    assert "Variation axis" in sections
    assert sections["Variation axis"] == "poses only"
```

- [ ] **Step 2: Run tests — they fail because pb.state does not exist**

Run: `pytest tests/test_state.py -v`
Expected: ImportError for `pb.state`.

- [ ] **Step 3: Create `pb/state.py` with the extracted helpers**

```python
"""Project / subject / variant state and style helpers.

Path resolution, subjects.yaml loading, and style.md parsing — all the
pieces of pb that are about the on-disk shape of a project, kept out of
cli.py so commands stay thin.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import click
import yaml


def _root() -> Path:
    return Path(os.environ.get("PB_ROOT", Path.cwd()))


def _projects() -> Path:
    return _root() / "projects"


def project_dir(project: str) -> Path:
    return _projects() / project


def style_path(project: str) -> Path:
    return project_dir(project) / "style.md"


def subjects_path(project: str) -> Path:
    return project_dir(project) / "subjects.yaml"


def variant_dir(project: str, subject: str, variant: str) -> Path:
    return project_dir(project) / subject / variant


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
```

- [ ] **Step 4: Update `pb/cli.py` to import from `pb.state`**

In `pb/cli.py`, delete the helpers section (lines defining `ROOT`, `PROJECTS`, `project_dir`, `style_path`, `subjects_path`, `require_project`, `load_subjects`, `find_subject`, `parse_style`) and add the import at the top:

```python
from pb.state import (
    project_dir,
    style_path,
    subjects_path,
    variant_dir,
    require_project,
    load_subjects,
    find_subject,
    parse_style,
)
```

Keep `TEMPLATES`, `MESHY_API_BASE`, `MESHY_ENDPOINT` constants in cli.py for now (they move in later tasks).

- [ ] **Step 5: Run all tests — must pass**

Run: `pytest -v`
Expected: all 7 state tests pass.

- [ ] **Step 6: Verify CLI still works**

Run: `pb --help`
Expected: existing command list (init, prompt, stage, meshify, learn, list) renders without error.

- [ ] **Step 7: Commit**

```bash
git add pb/cli.py pb/state.py tests/test_state.py
git commit -m "refactor: extract state and path helpers to pb/state.py"
```

---

### Task 3: Define backend protocol & registry

**Files:**
- Create: `pb/backends/__init__.py`
- Test: `tests/test_backends_registry.py`

- [ ] **Step 1: Write failing tests for the registry**

Create `tests/test_backends_registry.py`:

```python
"""Tests for the backend registry — protocol shape and lookup."""

from __future__ import annotations

from pathlib import Path

import pytest

from pb import backends


def test_get_unknown_backend_raises():
    with pytest.raises(KeyError, match="unknown"):
        backends.get("nope-not-a-real-backend")


def test_register_and_get_roundtrip():
    class FakeBackend:
        name = "fake-test"
        env_key = "FAKE_API_KEY"
        accepted_views = {"front"}

        def submit(self, views, params):
            return {"task_id": "t1", "preview_url": "http://x"}

        def status(self, task_id):
            return {"state": "ready", "model_urls": {"stl": "http://y"}, "error": None}

        def fetch(self, task_id, model_urls):
            return b"fake-stl"

    backends.register(FakeBackend())
    got = backends.get("fake-test")
    assert got.name == "fake-test"
    assert got.submit({"front": Path("/tmp/x.png")}, {})["task_id"] == "t1"


def test_list_backends_includes_registered():
    names = backends.list_names()
    assert "fake-test" in names
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_backends_registry.py -v`
Expected: ImportError for `pb.backends`.

- [ ] **Step 3: Create `pb/backends/__init__.py`**

```python
"""Mesh-backend adapter protocol + registry.

Each backend (Meshy, Hi3D, ...) lives in its own module under pb/backends/
and registers an instance via `register()`. The CLI looks up adapters by
name via `get()`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TypedDict, runtime_checkable


class SubmitResult(TypedDict):
    task_id: str
    preview_url: str


class StatusResult(TypedDict):
    state: str  # "pending" | "ready" | "failed"
    model_urls: dict[str, str] | None
    error: str | None


@runtime_checkable
class MeshBackend(Protocol):
    name: str
    env_key: str
    accepted_views: set[str]

    def submit(
        self, views: dict[str, Path], params: dict[str, str]
    ) -> SubmitResult: ...

    def status(self, task_id: str) -> StatusResult: ...

    def fetch(self, task_id: str, model_urls: dict[str, str]) -> bytes: ...


_BACKENDS: dict[str, MeshBackend] = {}


def register(backend: MeshBackend) -> None:
    _BACKENDS[backend.name] = backend


def get(name: str) -> MeshBackend:
    if name not in _BACKENDS:
        raise KeyError(f"unknown backend: {name!r} (known: {sorted(_BACKENDS)})")
    return _BACKENDS[name]


def list_names() -> list[str]:
    return sorted(_BACKENDS)
```

- [ ] **Step 4: Run tests — pass**

Run: `pytest tests/test_backends_registry.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add pb/backends/__init__.py tests/test_backends_registry.py
git commit -m "feat: add MeshBackend protocol and registry"
```

---

### Task 4: Extract Meshy adapter to `pb/backends/meshy.py`

**Files:**
- Create: `pb/backends/meshy.py`
- Test: `tests/test_backends_meshy.py`
- Modify: `pb/cli.py` (remove now-extracted Meshy constants)

- [ ] **Step 1: Write failing tests for Meshy adapter**

Create `tests/test_backends_meshy.py`:

```python
"""Tests for the Meshy backend adapter — submit / status / fetch with mocked HTTP."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pb.backends import meshy


@pytest.fixture
def meshy_backend(monkeypatch):
    monkeypatch.setenv("MESHY_API_KEY", "sk-test")
    return meshy.MeshyBackend()


def test_protocol_attributes(meshy_backend):
    assert meshy_backend.name == "meshy"
    assert meshy_backend.env_key == "MESHY_API_KEY"
    assert "front" in meshy_backend.accepted_views
    assert "back" in meshy_backend.accepted_views


def test_submit_posts_views_and_returns_task(meshy_backend, tmp_path):
    front = tmp_path / "front.png"
    back = tmp_path / "back.png"
    front.write_bytes(b"\x89PNG\r\n\x1a\nfront-bytes")
    back.write_bytes(b"\x89PNG\r\n\x1a\nback-bytes")

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"result": "task-xyz"}

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = fake_response

        result = meshy_backend.submit(
            {"front": front, "back": back},
            {"topology": "triangle", "should_remesh": "true"},
        )

    assert result["task_id"] == "task-xyz"
    assert "task-xyz" in result["preview_url"]
    args, kwargs = mock_client.post.call_args
    body = kwargs["json"]
    assert len(body["image_urls"]) == 2
    assert body["topology"] == "triangle"
    assert body["should_remesh"] is True  # coerced from "true"


def test_submit_drops_unsupported_views(meshy_backend, tmp_path, capsys):
    front = tmp_path / "front.png"
    weird = tmp_path / "isometric.png"
    front.write_bytes(b"\x89PNG")
    weird.write_bytes(b"\x89PNG")

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = MagicMock(status_code=200, json=lambda: {"result": "t"})
        meshy_backend.submit({"front": front, "isometric": weird}, {})

    captured = capsys.readouterr()
    assert "isometric" in captured.err.lower() or "isometric" in captured.out.lower()


def test_submit_rejects_unknown_param(meshy_backend, tmp_path):
    front = tmp_path / "front.png"
    front.write_bytes(b"\x89PNG")
    with pytest.raises(ValueError, match="unknown param"):
        meshy_backend.submit({"front": front}, {"not_a_real_param": "x"})


def test_status_pending(meshy_backend):
    fake = MagicMock(status_code=200)
    fake.json.return_value = {"status": "IN_PROGRESS", "progress": 50}
    fake.raise_for_status.return_value = None
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = fake
        result = meshy_backend.status("task-xyz")
    assert result["state"] == "pending"
    assert result["model_urls"] is None


def test_status_ready_returns_model_urls(meshy_backend):
    fake = MagicMock(status_code=200)
    fake.json.return_value = {
        "status": "SUCCEEDED",
        "model_urls": {"stl": "https://meshy.example/stl", "glb": "https://meshy.example/glb"},
    }
    fake.raise_for_status.return_value = None
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = fake
        result = meshy_backend.status("task-xyz")
    assert result["state"] == "ready"
    assert result["model_urls"]["stl"] == "https://meshy.example/stl"


def test_status_failed(meshy_backend):
    fake = MagicMock(status_code=200)
    fake.json.return_value = {"status": "FAILED", "task_error": "no thx"}
    fake.raise_for_status.return_value = None
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = fake
        result = meshy_backend.status("task-xyz")
    assert result["state"] == "failed"
    assert "no thx" in result["error"]


def test_fetch_downloads_stl(meshy_backend):
    fake = MagicMock()
    fake.content = b"stl-bytes"
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = fake
        data = meshy_backend.fetch("task-xyz", {"stl": "https://meshy.example/stl"})
    assert data == b"stl-bytes"


def test_missing_api_key_at_submit(monkeypatch, tmp_path):
    monkeypatch.delenv("MESHY_API_KEY", raising=False)
    backend = meshy.MeshyBackend()
    front = tmp_path / "front.png"
    front.write_bytes(b"\x89PNG")
    with pytest.raises(RuntimeError, match="MESHY_API_KEY"):
        backend.submit({"front": front}, {})


def test_registered_in_global_registry():
    from pb import backends
    assert "meshy" in backends.list_names()
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_backends_meshy.py -v`
Expected: ImportError for `pb.backends.meshy`.

- [ ] **Step 3: Create `pb/backends/meshy.py`**

```python
"""Meshy multi-image-to-3D adapter."""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import click
import httpx

from pb import backends


MESHY_API_BASE = "https://api.meshy.ai/openapi/v1"
MESHY_ENDPOINT = f"{MESHY_API_BASE}/multi-image-to-3d"

# Param keys this adapter accepts, with their target Python type.
_PARAM_SCHEMA: dict[str, type] = {
    "topology": str,
    "should_remesh": bool,
    "ai_model": str,
}

_BOOL_TRUE = {"true", "1", "yes", "y", "on"}
_BOOL_FALSE = {"false", "0", "no", "n", "off"}


def _coerce(key: str, value: str):
    target = _PARAM_SCHEMA[key]
    if target is bool:
        v = value.strip().lower()
        if v in _BOOL_TRUE:
            return True
        if v in _BOOL_FALSE:
            return False
        raise ValueError(f"param '{key}' expects a boolean, got {value!r}")
    return target(value)


def _to_data_uri(path: Path) -> str:
    b = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/png;base64,{b}"


class MeshyBackend:
    name = "meshy"
    env_key = "MESHY_API_KEY"
    accepted_views: set[str] = {"front", "back", "left", "right"}

    def _api_key(self) -> str:
        key = os.environ.get(self.env_key)
        if not key:
            raise RuntimeError(f"{self.env_key} not set in environment")
        return key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key()}"}

    def submit(self, views: dict[str, Path], params: dict[str, str]):
        # Validate params first so we fail before any network call.
        for key in params:
            if key not in _PARAM_SCHEMA:
                raise ValueError(
                    f"unknown param {key!r} for meshy "
                    f"(accepted: {sorted(_PARAM_SCHEMA)})"
                )
        coerced = {k: _coerce(k, v) for k, v in params.items()}

        used = {label: path for label, path in views.items() if label in self.accepted_views}
        dropped = sorted(set(views) - set(used))
        for label in dropped:
            click.echo(
                f"meshy: dropping view {label!r} (not in accepted_views={sorted(self.accepted_views)})",
                err=True,
            )

        if not used:
            raise ValueError(
                f"meshy: no usable views (accepted_views={sorted(self.accepted_views)})"
            )

        payload = {
            "image_urls": [_to_data_uri(p) for p in used.values()],
            "ai_model": coerced.get("ai_model", "meshy-5"),
            "topology": coerced.get("topology", "triangle"),
            "should_remesh": coerced.get("should_remesh", True),
        }

        with httpx.Client(timeout=60) as client:
            r = client.post(MESHY_ENDPOINT, json=payload, headers=self._headers())
            if r.status_code >= 300:
                raise RuntimeError(f"meshy submit failed: {r.status_code} {r.text}")
            task_id = r.json()["result"]

        return {
            "task_id": task_id,
            "preview_url": f"https://www.meshy.ai/discover/{task_id}",
        }

    def status(self, task_id: str):
        with httpx.Client(timeout=60) as client:
            r = client.get(f"{MESHY_ENDPOINT}/{task_id}", headers=self._headers())
            r.raise_for_status()
            task = r.json()

        s = task.get("status")
        if s == "SUCCEEDED":
            return {
                "state": "ready",
                "model_urls": task.get("model_urls"),
                "error": None,
            }
        if s in ("FAILED", "CANCELED", "EXPIRED"):
            return {
                "state": "failed",
                "model_urls": None,
                "error": str(task.get("task_error")),
            }
        return {"state": "pending", "model_urls": None, "error": None}

    def fetch(self, task_id: str, model_urls: dict[str, str]) -> bytes:
        url = model_urls.get("stl") or model_urls.get("glb") or model_urls.get("fbx")
        if not url:
            raise RuntimeError(f"meshy: no usable model URL in {model_urls}")
        with httpx.Client(timeout=60) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.content


backends.register(MeshyBackend())
```

- [ ] **Step 4: Force registry to load the meshy module**

Edit `pb/backends/__init__.py` to import meshy at the bottom (after the registry definitions) so the side-effect `register(...)` runs:

```python
# (append at bottom of pb/backends/__init__.py)

from pb.backends import meshy as _meshy  # noqa: F401  — registers MeshyBackend
```

- [ ] **Step 5: Run tests — pass**

Run: `pytest tests/test_backends_meshy.py -v`
Expected: all 10 tests pass.

- [ ] **Step 6: Run full suite to confirm no regression**

Run: `pytest -v`
Expected: all tests pass.

- [ ] **Step 7: Remove old Meshy constants from cli.py**

In `pb/cli.py`, delete the lines:

```python
MESHY_API_BASE = "https://api.meshy.ai/openapi/v1"
MESHY_ENDPOINT = f"{MESHY_API_BASE}/multi-image-to-3d"
```

(They are now in `pb/backends/meshy.py`. The `meshify` command still references them — Task 11 onwards rewrites that path, so leaving meshify temporarily broken-on-runtime is acceptable; tests don't exercise it. If you want to keep meshify functional in this commit, leave the constants for now and remove them in Task 15.)

For safety in this plan: leave the constants in `cli.py` for now. Remove them in Task 15.

- [ ] **Step 8: Commit**

```bash
git add pb/backends/__init__.py pb/backends/meshy.py tests/test_backends_meshy.py
git commit -m "feat: extract Meshy adapter to pb/backends/meshy.py"
```

---

### Task 5: Add Hi3D stub adapter

The Hi3D adapter validates the protocol shape against more than just Meshy. Real implementation is deferred until the user has Hi3D credentials and a target endpoint.

**Files:**
- Create: `pb/backends/hi3d.py`
- Test: `tests/test_backends_hi3d.py`
- Modify: `pb/backends/__init__.py` (import hi3d)

- [ ] **Step 1: Write failing tests**

Create `tests/test_backends_hi3d.py`:

```python
"""Tests for Hi3D stub adapter — fails clearly until real implementation lands."""

from __future__ import annotations

from pathlib import Path

import pytest

from pb.backends import hi3d


def test_protocol_attributes():
    b = hi3d.Hi3DBackend()
    assert b.name == "hi3d"
    assert b.env_key == "HI3D_API_KEY"
    assert isinstance(b.accepted_views, set)


def test_submit_raises_not_implemented(tmp_path):
    b = hi3d.Hi3DBackend()
    front = tmp_path / "front.png"
    front.write_bytes(b"\x89PNG")
    with pytest.raises(NotImplementedError, match="Hi3D"):
        b.submit({"front": front}, {})


def test_status_raises_not_implemented():
    b = hi3d.Hi3DBackend()
    with pytest.raises(NotImplementedError, match="Hi3D"):
        b.status("task-xyz")


def test_fetch_raises_not_implemented():
    b = hi3d.Hi3DBackend()
    with pytest.raises(NotImplementedError, match="Hi3D"):
        b.fetch("task-xyz", {})


def test_registered_in_global_registry():
    from pb import backends
    assert "hi3d" in backends.list_names()
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_backends_hi3d.py -v`
Expected: ImportError for `pb.backends.hi3d`.

- [ ] **Step 3: Create `pb/backends/hi3d.py`**

```python
"""Hi3D adapter — stub.

Validates that the MeshBackend protocol holds for more than one provider.
Real implementation lands when the user has Hi3D credentials and an
endpoint to point at.
"""

from __future__ import annotations

from pathlib import Path

from pb import backends


class Hi3DBackend:
    name = "hi3d"
    env_key = "HI3D_API_KEY"
    accepted_views: set[str] = {"front", "back"}

    def submit(self, views: dict[str, Path], params: dict[str, str]):
        raise NotImplementedError(
            "Hi3D adapter is a stub. "
            "Add real submit logic in pb/backends/hi3d.py when Hi3D credentials and endpoint are available."
        )

    def status(self, task_id: str):
        raise NotImplementedError(
            "Hi3D adapter is a stub — see submit() for next steps."
        )

    def fetch(self, task_id: str, model_urls: dict[str, str]) -> bytes:
        raise NotImplementedError(
            "Hi3D adapter is a stub — see submit() for next steps."
        )


backends.register(Hi3DBackend())
```

- [ ] **Step 4: Wire up the import in `pb/backends/__init__.py`**

Append to `pb/backends/__init__.py`:

```python
from pb.backends import hi3d as _hi3d  # noqa: F401  — registers Hi3DBackend
```

- [ ] **Step 5: Run tests — pass**

Run: `pytest tests/test_backends_hi3d.py -v`
Expected: 5 passed.

- [ ] **Step 6: Run full suite**

Run: `pytest -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add pb/backends/__init__.py pb/backends/hi3d.py tests/test_backends_hi3d.py
git commit -m "feat: add Hi3D stub adapter to validate backend protocol"
```

---

### Task 6: Cropper crop function (pure, no server)

The cropper's PIL logic is a pure function. Test it independently before wiring HTTP.

**Files:**
- Create: `pb/cropper.py`
- Test: `tests/test_cropper_crop.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cropper_crop.py`:

```python
"""Tests for pb.cropper.crop_to_files — produces per-view PNGs and regions.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from pb import cropper


def test_crop_to_files_writes_per_view_pngs(variant):
    regions = {
        "front": [0, 0, 100, 100],   # x, y, w, h
        "back":  [100, 0, 100, 100],
    }
    cropper.crop_to_files(variant / "source.png", regions, variant)

    front = Image.open(variant / "front.png")
    back = Image.open(variant / "back.png")
    assert front.size == (100, 100)
    assert back.size == (100, 100)


def test_crop_to_files_writes_regions_json(variant):
    regions = {"front": [10, 20, 50, 30]}
    cropper.crop_to_files(variant / "source.png", regions, variant)
    saved = json.loads((variant / "regions.json").read_text())
    assert saved == {"front": [10, 20, 50, 30]}


def test_crop_rejects_zero_area(variant):
    with pytest.raises(ValueError, match="zero-area"):
        cropper.crop_to_files(variant / "source.png", {"front": [0, 0, 0, 50]}, variant)


def test_crop_rejects_out_of_bounds(variant):
    # variant fixture source is 200x100
    with pytest.raises(ValueError, match="out of bounds"):
        cropper.crop_to_files(variant / "source.png", {"front": [150, 0, 100, 100]}, variant)


def test_crop_rejects_empty_regions(variant):
    with pytest.raises(ValueError, match="at least one"):
        cropper.crop_to_files(variant / "source.png", {}, variant)


def test_crop_arbitrary_label(variant):
    cropper.crop_to_files(variant / "source.png", {"isometric": [0, 0, 50, 50]}, variant)
    assert (variant / "isometric.png").exists()
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_cropper_crop.py -v`
Expected: ImportError for `pb.cropper`.

- [ ] **Step 3: Create `pb/cropper.py` with `crop_to_files`**

```python
"""Localhost region cropper.

Two pieces:
  - crop_to_files: pure function — given a source image + labelled regions,
    write per-view PNGs and a regions.json into a target dir.
  - start_server (Task 7): tiny stdlib http.server that hosts the cropping UI
    and POSTs back to crop_to_files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image


_LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def crop_to_files(source: Path, regions: dict[str, list[int]], out_dir: Path) -> None:
    """Crop source.png into one PNG per labelled region; write regions.json.

    `regions` maps label -> [x, y, w, h] (ints, source pixel coordinates).
    """
    if not regions:
        raise ValueError("crop requires at least one labelled region")

    img = Image.open(source)
    width, height = img.size

    for label, box in regions.items():
        if not _LABEL_RE.match(label):
            raise ValueError(
                f"invalid label {label!r}: must match [a-z][a-z0-9_-]*"
            )
        if len(box) != 4:
            raise ValueError(f"region {label!r} must be [x, y, w, h]")
        x, y, w, h = box
        if w <= 0 or h <= 0:
            raise ValueError(f"region {label!r} has zero-area or negative dims: {box}")
        if x < 0 or y < 0 or x + w > width or y + h > height:
            raise ValueError(
                f"region {label!r} {box} out of bounds for {width}x{height} source"
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    for label, (x, y, w, h) in regions.items():
        crop = img.crop((x, y, x + w, y + h))
        crop.save(out_dir / f"{label}.png", format="PNG")

    (out_dir / "regions.json").write_text(json.dumps(regions, indent=2) + "\n")
```

- [ ] **Step 4: Run tests — pass**

Run: `pytest tests/test_cropper_crop.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add pb/cropper.py tests/test_cropper_crop.py
git commit -m "feat: cropper crop_to_files — labelled-region PIL crops + regions.json"
```

---

### Task 7: Cropper HTTP server

**Files:**
- Modify: `pb/cropper.py` (add server)
- Test: `tests/test_cropper_server.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cropper_server.py`:

```python
"""Tests for pb.cropper.start_server — routes and shutdown behaviour."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from pb import cropper


def _post_json(url: str, body: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.read()


def test_get_root_returns_html(variant):
    server = cropper.start_server(variant, port=0)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        status, body = _get(url)
        assert status == 200
        assert b"<html" in body.lower()
        assert b"canvas" in body.lower() or b"img" in body.lower()
    finally:
        server.shutdown()
        server.server_close()


def test_get_source_returns_png(variant):
    server = cropper.start_server(variant, port=0)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/source.png"
        status, body = _get(url)
        assert status == 200
        assert body[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        server.shutdown()
        server.server_close()


def test_get_regions_when_absent_returns_empty(variant):
    server = cropper.start_server(variant, port=0)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/regions"
        status, body = _get(url)
        assert status == 200
        assert json.loads(body) == {}
    finally:
        server.shutdown()
        server.server_close()


def test_post_save_writes_files_and_signals_shutdown(variant):
    server = cropper.start_server(variant, port=0)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/save"
        status, _body = _post_json(url, {"regions": {"front": [0, 0, 50, 50]}})
        assert status == 200
        assert (variant / "front.png").exists()
        assert (variant / "regions.json").exists()
        assert server.save_event.is_set()
    finally:
        server.shutdown()
        server.server_close()


def test_post_save_with_empty_regions_returns_400(variant):
    server = cropper.start_server(variant, port=0)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/save"
        status, body = _post_json(url, {"regions": {}})
        assert status == 400
        assert b"at least one" in body.lower()
    finally:
        server.shutdown()
        server.server_close()


def test_run_until_save_blocks_then_returns(variant):
    """The high-level entry point: starts the server, blocks until /save POST, exits."""
    saved_box = {}

    def post_when_ready():
        # Wait briefly for the server to start, then POST.
        import time, urllib.request
        time.sleep(0.2)
        url = f"http://127.0.0.1:{saved_box['port']}/save"
        _post_json(url, {"regions": {"front": [0, 0, 80, 80]}})

    server = cropper.start_server(variant, port=0)
    saved_box["port"] = server.server_address[1]
    threading.Thread(target=post_when_ready, daemon=True).start()
    cropper.run_until_save(server, timeout=5.0)

    assert (variant / "front.png").exists()
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_cropper_server.py -v`
Expected: AttributeError for `start_server` / `run_until_save`.

- [ ] **Step 3: Add the server to `pb/cropper.py`**

Append to `pb/cropper.py`:

```python
import json as _json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from importlib.resources import files as _resource_files


def _index_html() -> bytes:
    return (_resource_files("pb") / "cropper_assets" / "index.html").read_bytes()


class _CropperServer(ThreadingHTTPServer):
    def __init__(self, addr, handler_cls, variant_dir: Path):
        super().__init__(addr, handler_cls)
        self.variant_dir = variant_dir
        self.save_event = threading.Event()
        self.last_error: str | None = None


class _CropperHandler(BaseHTTPRequestHandler):
    server: _CropperServer  # type narrowing

    def log_message(self, fmt, *args):  # silence default access log
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(200, _index_html(), "text/html; charset=utf-8")
            return
        if self.path == "/source.png":
            self._send(200, (self.server.variant_dir / "source.png").read_bytes(), "image/png")
            return
        if self.path == "/regions":
            path = self.server.variant_dir / "regions.json"
            payload = path.read_bytes() if path.exists() else b"{}"
            self._send(200, payload, "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/save":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = _json.loads(self.rfile.read(length))
            regions = payload.get("regions") or {}
            crop_to_files(
                self.server.variant_dir / "source.png",
                regions,
                self.server.variant_dir,
            )
        except ValueError as e:
            self._send(400, str(e).encode(), "text/plain")
            return
        except Exception as e:
            self._send(500, str(e).encode(), "text/plain")
            return
        self._send(200, b"ok", "text/plain")
        self.server.save_event.set()


def start_server(variant_dir: Path, port: int = 0) -> _CropperServer:
    """Create and start a cropper server in a background thread.

    Returns the server object; caller is responsible for calling
    `run_until_save` (blocks on save POST) or `server.shutdown()`.
    """
    server = _CropperServer(("127.0.0.1", port), _CropperHandler, variant_dir)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def run_until_save(server: _CropperServer, timeout: float = 600.0) -> None:
    """Block until /save POST sets save_event, then shut down the server."""
    try:
        if not server.save_event.wait(timeout=timeout):
            raise TimeoutError(f"cropper: no save within {timeout}s")
    finally:
        server.shutdown()
        server.server_close()
```

- [ ] **Step 4: Add a placeholder index.html so the server tests can run**

Create `pb/cropper_assets/index.html` (Task 8 fleshes out the JS, but tests in Task 7 only need the file to exist with `<html>` and `<canvas>` markers):

```html
<!doctype html>
<html>
<head><meta charset="utf-8"><title>pb cropper</title></head>
<body>
<canvas id="cv"></canvas>
<img id="src" src="/source.png" hidden>
<!-- Task 8 fills in JS -->
</body>
</html>
```

- [ ] **Step 5: Run tests — pass**

Run: `pytest tests/test_cropper_server.py -v`
Expected: 6 passed.

- [ ] **Step 6: Run full suite**

Run: `pytest -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add pb/cropper.py pb/cropper_assets/index.html tests/test_cropper_server.py
git commit -m "feat: cropper HTTP server — routes, save signalling, run_until_save"
```

---

### Task 8: Cropper HTML page (vanilla JS rectangle drawer)

No automated tests — verified manually after the `pb crop` command lands in Task 11. This task ships the working UI.

**Files:**
- Modify: `pb/cropper_assets/index.html`

- [ ] **Step 1: Write the full UI**

Replace `pb/cropper_assets/index.html` with:

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>pb cropper</title>
  <style>
    body { margin: 0; font-family: ui-monospace, monospace; background: #111; color: #ddd; }
    header { padding: 8px 12px; background: #1a1a1a; display: flex; gap: 12px; align-items: center; }
    header button { padding: 4px 12px; background: #2a7; color: #111; border: 0; cursor: pointer; font: inherit; }
    header button:disabled { opacity: 0.4; cursor: not-allowed; }
    header .hint { color: #888; font-size: 12px; }
    main { padding: 12px; display: flex; gap: 16px; }
    .canvas-wrap { position: relative; border: 1px solid #333; }
    canvas { display: block; cursor: crosshair; }
    aside { width: 280px; }
    .region { background: #1a1a1a; padding: 6px 8px; margin-bottom: 6px; display: flex; gap: 6px; align-items: center; }
    .region select, .region input[type=text] { background: #222; color: #ddd; border: 1px solid #333; padding: 2px 4px; font: inherit; }
    .region button { background: #444; color: #ddd; border: 0; padding: 2px 6px; cursor: pointer; }
    .region .swatch { width: 12px; height: 12px; }
  </style>
</head>
<body>
<header>
  <strong>pb cropper</strong>
  <span class="hint">drag to draw a region; pick a label; save when done</span>
  <button id="save" disabled>save</button>
</header>
<main>
  <div class="canvas-wrap">
    <canvas id="cv"></canvas>
  </div>
  <aside>
    <div id="regions"></div>
  </aside>
</main>
<script>
const COMMON_LABELS = ["front", "back", "left", "right", "top", "custom"];
const COLORS = ["#2a7", "#e74", "#5af", "#fd5", "#a8f", "#f8a"];

const cv = document.getElementById("cv");
const ctx = cv.getContext("2d");
const saveBtn = document.getElementById("save");
const regionsPanel = document.getElementById("regions");

let img = new Image();
let regions = []; // [{label, custom, x, y, w, h}]
let dragging = null;

img.onload = () => {
  cv.width = img.naturalWidth;
  cv.height = img.naturalHeight;
  draw();
};
img.src = "/source.png";

fetch("/regions").then(r => r.json()).then(saved => {
  for (const [label, box] of Object.entries(saved)) {
    regions.push({label: COMMON_LABELS.includes(label) ? label : "custom",
                  custom: COMMON_LABELS.includes(label) ? "" : label,
                  x: box[0], y: box[1], w: box[2], h: box[3]});
  }
  renderRegions();
  draw();
});

function effectiveLabel(r) { return r.label === "custom" ? r.custom.trim() : r.label; }

function draw() {
  if (!img.complete) return;
  ctx.drawImage(img, 0, 0);
  regions.forEach((r, i) => {
    ctx.lineWidth = 2;
    ctx.strokeStyle = COLORS[i % COLORS.length];
    ctx.strokeRect(r.x, r.y, r.w, r.h);
    ctx.fillStyle = COLORS[i % COLORS.length];
    ctx.font = "14px ui-monospace, monospace";
    ctx.fillText(effectiveLabel(r) || "(unnamed)", r.x + 4, r.y + 16);
  });
  if (dragging) {
    ctx.strokeStyle = "#fff";
    ctx.setLineDash([4, 4]);
    ctx.strokeRect(dragging.x, dragging.y, dragging.w, dragging.h);
    ctx.setLineDash([]);
  }
  saveBtn.disabled = regions.length === 0 ||
    regions.some(r => !effectiveLabel(r) || r.w <= 0 || r.h <= 0);
}

function pos(e) {
  const rect = cv.getBoundingClientRect();
  const sx = cv.width / rect.width;
  const sy = cv.height / rect.height;
  return {x: Math.round((e.clientX - rect.left) * sx),
          y: Math.round((e.clientY - rect.top) * sy)};
}

cv.addEventListener("mousedown", e => {
  const p = pos(e);
  dragging = {x: p.x, y: p.y, w: 0, h: 0, startX: p.x, startY: p.y};
});
cv.addEventListener("mousemove", e => {
  if (!dragging) return;
  const p = pos(e);
  dragging.x = Math.min(p.x, dragging.startX);
  dragging.y = Math.min(p.y, dragging.startY);
  dragging.w = Math.abs(p.x - dragging.startX);
  dragging.h = Math.abs(p.y - dragging.startY);
  draw();
});
cv.addEventListener("mouseup", () => {
  if (dragging && dragging.w > 4 && dragging.h > 4) {
    regions.push({label: COMMON_LABELS[regions.length % (COMMON_LABELS.length - 1)],
                  custom: "", x: dragging.x, y: dragging.y, w: dragging.w, h: dragging.h});
    renderRegions();
  }
  dragging = null;
  draw();
});

function renderRegions() {
  regionsPanel.innerHTML = "";
  regions.forEach((r, i) => {
    const row = document.createElement("div");
    row.className = "region";
    row.innerHTML = `
      <span class="swatch" style="background: ${COLORS[i % COLORS.length]}"></span>
      <select data-i="${i}" class="lbl">
        ${COMMON_LABELS.map(l => `<option value="${l}" ${l === r.label ? "selected" : ""}>${l}</option>`).join("")}
      </select>
      <input type="text" data-i="${i}" class="custom" placeholder="custom label" value="${r.custom}" ${r.label !== "custom" ? "hidden" : ""}>
      <button data-i="${i}" class="rm">x</button>
    `;
    regionsPanel.appendChild(row);
  });
  regionsPanel.querySelectorAll(".lbl").forEach(s => s.onchange = e => {
    regions[+e.target.dataset.i].label = e.target.value;
    renderRegions(); draw();
  });
  regionsPanel.querySelectorAll(".custom").forEach(inp => inp.oninput = e => {
    regions[+e.target.dataset.i].custom = e.target.value;
    draw();
  });
  regionsPanel.querySelectorAll(".rm").forEach(b => b.onclick = e => {
    regions.splice(+e.target.dataset.i, 1);
    renderRegions(); draw();
  });
}

saveBtn.onclick = async () => {
  saveBtn.disabled = true;
  const payload = {regions: {}};
  for (const r of regions) {
    payload.regions[effectiveLabel(r)] = [r.x, r.y, r.w, r.h];
  }
  const res = await fetch("/save", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    document.body.innerHTML = "<main style='padding:24px'>saved. you can close this tab.</main>";
  } else {
    alert("save failed: " + (await res.text()));
    saveBtn.disabled = false;
  }
};
</script>
</body>
</html>
```

- [ ] **Step 2: Re-run server tests to confirm the more elaborate HTML still serves**

Run: `pytest tests/test_cropper_server.py -v`
Expected: 6 passed (the markup checks still hold — `<canvas>` and `<html>` are present).

- [ ] **Step 3: Commit**

```bash
git add pb/cropper_assets/index.html
git commit -m "feat: cropper UI — vanilla JS labelled-region drawer"
```

---

### Task 9: `pb crop` command

**Files:**
- Modify: `pb/cli.py`
- Test: `tests/test_cli_crop.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_crop.py`:

```python
"""Tests for `pb crop` — auto-creates variant dir, copies source, opens cropper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from pb.cli import cli


def test_crop_auto_creates_variant_and_copies_source(project, tmp_path):
    src = tmp_path / "external.png"
    # Reuse fixture's PNG-generating helper indirectly:
    from tests.conftest import _solid_png
    src.write_bytes(_solid_png(120, 80))

    with patch("pb.cli.cropper.start_server") as mock_start, \
         patch("pb.cli.cropper.run_until_save") as mock_run, \
         patch("pb.cli.webbrowser.open"):
        mock_server = MagicMock()
        mock_server.server_address = ("127.0.0.1", 12345)
        mock_start.return_value = mock_server

        runner = CliRunner()
        result = runner.invoke(cli, ["crop", "soviets", "at-rifle-team", "v1", str(src)])

    assert result.exit_code == 0, result.output
    variant = project / "at-rifle-team" / "v1"
    assert variant.is_dir()
    assert (variant / "source.png").exists()
    assert (variant / "source.png").read_bytes() == src.read_bytes()
    mock_start.assert_called_once()
    mock_run.assert_called_once()
    assert "12345" in result.output  # URL printed


def test_crop_rejects_unknown_subject(project, tmp_path):
    src = tmp_path / "external.png"
    src.write_bytes(b"\x89PNG")
    runner = CliRunner()
    result = runner.invoke(cli, ["crop", "soviets", "no-such-subject", "v1", str(src)])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_crop_rejects_missing_source(project):
    runner = CliRunner()
    result = runner.invoke(cli, ["crop", "soviets", "at-rifle-team", "v1", "/no/such/file.png"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "no such" in result.output.lower()
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_cli_crop.py -v`
Expected: error, no `crop` command.

- [ ] **Step 3: Implement `pb crop`**

In `pb/cli.py`, add at the top of the imports:

```python
import shutil
import webbrowser
from pb import cropper
```

Then add the command (place after `prompt`):

```python
@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def crop(project: str, subject: str, variant: str, source: Path) -> None:
    """Open a localhost cropper to slice SOURCE into labelled views."""
    require_project(project)
    find_subject(project, subject)

    target = variant_dir(project, subject, variant)
    target.mkdir(parents=True, exist_ok=True)

    dest = target / "source.png"
    if dest.resolve() != source.resolve():
        shutil.copyfile(source, dest)

    server = cropper.start_server(target, port=0)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    click.echo(f"cropper running at {url}")
    click.echo("draw labelled regions, click save, this command will exit")
    webbrowser.open(url)
    cropper.run_until_save(server)
    click.echo(f"✓ crops written to {target}")
```

- [ ] **Step 4: Run tests — pass**

Run: `pytest tests/test_cli_crop.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add pb/cli.py tests/test_cli_crop.py
git commit -m "feat: pb crop — open localhost cropper, write labelled views"
```

---

### Task 10: `pb upload` command

**Files:**
- Modify: `pb/cli.py`
- Test: `tests/test_cli_upload.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_upload.py`:

```python
"""Tests for `pb upload` — submit views to backend, write task.json, exit."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from pb.cli import cli


@pytest.fixture
def cropped_variant(variant):
    """Variant with front.png and back.png ready for upload."""
    (variant / "front.png").write_bytes(b"\x89PNG\r\n\x1a\nfront")
    (variant / "back.png").write_bytes(b"\x89PNG\r\n\x1a\nback")
    return variant


def test_upload_calls_backend_and_writes_task_json(cropped_variant):
    fake_backend = MagicMock()
    fake_backend.submit.return_value = {
        "task_id": "abc123",
        "preview_url": "https://meshy.example/abc123",
    }
    with patch("pb.cli.backends.get", return_value=fake_backend):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "upload", "soviets", "at-rifle-team", "v1",
            "--backend", "meshy",
            "--param", "topology=triangle",
        ])
    assert result.exit_code == 0, result.output
    assert "abc123" in result.output
    assert "preview" in result.output.lower()

    task = json.loads((cropped_variant / "task.json").read_text())
    assert task["task_id"] == "abc123"
    assert task["backend"] == "meshy"
    assert task["preview_url"] == "https://meshy.example/abc123"
    assert task["params"] == {"topology": "triangle"}
    assert set(task["views_used"]) == {"front", "back"}


def test_upload_with_no_views_fails(variant):
    runner = CliRunner()
    result = runner.invoke(cli, [
        "upload", "soviets", "at-rifle-team", "v1", "--backend", "meshy",
    ])
    assert result.exit_code != 0
    assert "no labelled views" in result.output.lower()


def test_upload_unknown_backend_fails(cropped_variant):
    runner = CliRunner()
    result = runner.invoke(cli, [
        "upload", "soviets", "at-rifle-team", "v1", "--backend", "imaginary",
    ])
    assert result.exit_code != 0
    assert "unknown backend" in result.output.lower()


def test_upload_passes_params_to_backend(cropped_variant):
    fake_backend = MagicMock()
    fake_backend.submit.return_value = {"task_id": "t", "preview_url": "u"}
    with patch("pb.cli.backends.get", return_value=fake_backend):
        runner = CliRunner()
        runner.invoke(cli, [
            "upload", "soviets", "at-rifle-team", "v1", "--backend", "meshy",
            "--param", "topology=quad",
            "--param", "should_remesh=false",
        ])
    args, _kwargs = fake_backend.submit.call_args
    _views, params = args
    assert params == {"topology": "quad", "should_remesh": "false"}
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_cli_upload.py -v`
Expected: no `upload` command.

- [ ] **Step 3: Implement `pb upload`**

In `pb/cli.py`, add at top of imports:

```python
import datetime as dt
import json
from pb import backends
```

(Keep the existing `import datetime as dt` if already present — don't duplicate.)

Add the command (after `crop`):

```python
def _parse_params(params: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in params:
        if "=" not in p:
            raise click.BadParameter(f"--param expects k=v, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def _collect_views(variant_path: Path) -> dict[str, Path]:
    """Find every <label>.png in the variant dir except source.png and model.stl-adjacent files."""
    excluded = {"source.png"}
    views: dict[str, Path] = {}
    for child in sorted(variant_path.iterdir()):
        if child.suffix.lower() != ".png":
            continue
        if child.name in excluded:
            continue
        label = child.stem
        views[label] = child
    return views


@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
@click.option("--backend", "backend_name", required=True, help="Mesh backend (e.g. meshy, hi3d).")
@click.option("--param", "params", multiple=True, help="Backend-specific k=v param. Repeatable.")
def upload(project: str, subject: str, variant: str, backend_name: str, params: tuple[str, ...]) -> None:
    """Submit labelled views to BACKEND. Writes task.json. Does not download."""
    require_project(project)
    find_subject(project, subject)

    target = variant_dir(project, subject, variant)
    if not target.is_dir():
        click.echo(f"error: variant dir does not exist: {target}", err=True)
        click.echo(f"hint: pb crop {project} {subject} {variant} <image>", err=True)
        sys.exit(1)

    views = _collect_views(target)
    if not views:
        click.echo(f"error: no labelled views (PNGs other than source.png) in {target}", err=True)
        sys.exit(1)

    try:
        backend = backends.get(backend_name)
    except KeyError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    parsed_params = _parse_params(params)

    try:
        result = backend.submit(views, parsed_params)
    except (ValueError, RuntimeError) as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    task_record = {
        "backend": backend_name,
        "task_id": result["task_id"],
        "preview_url": result["preview_url"],
        "submitted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "params": parsed_params,
        "views_used": sorted(set(views) & set(backend.accepted_views)),
        "last_status": "pending",
    }
    (target / "task.json").write_text(json.dumps(task_record, indent=2) + "\n")

    click.echo(f"✓ submitted task {result['task_id']}")
    click.echo(f"  preview: {result['preview_url']}")
    click.echo("  evaluate the mesh, then run `pb fetch` to commit or `pb retry` to redo")
```

- [ ] **Step 4: Run tests — pass**

Run: `pytest tests/test_cli_upload.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add pb/cli.py tests/test_cli_upload.py
git commit -m "feat: pb upload — submit views to backend, write task.json"
```

---

### Task 11: `pb fetch` command

**Files:**
- Modify: `pb/cli.py`
- Test: `tests/test_cli_fetch.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_fetch.py`:

```python
"""Tests for `pb fetch` — read task.json, query status, download model only when ready."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from pb.cli import cli


@pytest.fixture
def variant_with_task(variant):
    (variant / "task.json").write_text(json.dumps({
        "backend": "meshy",
        "task_id": "abc123",
        "preview_url": "https://meshy.example/abc123",
        "submitted_at": "2026-05-15T17:00:00+00:00",
        "params": {},
        "views_used": ["front", "back"],
        "last_status": "pending",
    }))
    return variant


def test_fetch_ready_downloads_stl(variant_with_task):
    fake_backend = MagicMock()
    fake_backend.status.return_value = {
        "state": "ready",
        "model_urls": {"stl": "https://meshy.example/stl"},
        "error": None,
    }
    fake_backend.fetch.return_value = b"stl-payload"
    with patch("pb.cli.backends.get", return_value=fake_backend):
        runner = CliRunner()
        result = runner.invoke(cli, ["fetch", "soviets", "at-rifle-team", "v1"])
    assert result.exit_code == 0, result.output
    assert (variant_with_task / "model.stl").read_bytes() == b"stl-payload"

    saved = json.loads((variant_with_task / "task.json").read_text())
    assert saved["last_status"] == "ready"


def test_fetch_pending_exits_nonzero(variant_with_task):
    fake_backend = MagicMock()
    fake_backend.status.return_value = {"state": "pending", "model_urls": None, "error": None}
    with patch("pb.cli.backends.get", return_value=fake_backend):
        runner = CliRunner()
        result = runner.invoke(cli, ["fetch", "soviets", "at-rifle-team", "v1"])
    assert result.exit_code != 0
    assert "pending" in result.output.lower()
    assert not (variant_with_task / "model.stl").exists()


def test_fetch_failed_exits_nonzero(variant_with_task):
    fake_backend = MagicMock()
    fake_backend.status.return_value = {"state": "failed", "model_urls": None, "error": "broke"}
    with patch("pb.cli.backends.get", return_value=fake_backend):
        runner = CliRunner()
        result = runner.invoke(cli, ["fetch", "soviets", "at-rifle-team", "v1"])
    assert result.exit_code != 0
    assert "broke" in result.output.lower()
    saved = json.loads((variant_with_task / "task.json").read_text())
    assert saved["last_status"] == "failed"


def test_fetch_no_task_json(variant):
    runner = CliRunner()
    result = runner.invoke(cli, ["fetch", "soviets", "at-rifle-team", "v1"])
    assert result.exit_code != 0
    assert "no task.json" in result.output.lower() or "task.json" in result.output.lower()


def test_fetch_wait_polls_then_succeeds(variant_with_task):
    fake_backend = MagicMock()
    fake_backend.status.side_effect = [
        {"state": "pending", "model_urls": None, "error": None},
        {"state": "pending", "model_urls": None, "error": None},
        {"state": "ready", "model_urls": {"stl": "url"}, "error": None},
    ]
    fake_backend.fetch.return_value = b"stl"
    with patch("pb.cli.backends.get", return_value=fake_backend), \
         patch("pb.cli.time.sleep"):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "fetch", "soviets", "at-rifle-team", "v1",
            "--wait", "--poll-interval", "1", "--timeout", "10",
        ])
    assert result.exit_code == 0
    assert (variant_with_task / "model.stl").read_bytes() == b"stl"
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_cli_fetch.py -v`
Expected: no `fetch` command.

- [ ] **Step 3: Implement `pb fetch`**

In `pb/cli.py`, add at top of imports if not already present:

```python
import time
```

Add the command:

```python
@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
@click.option("--wait", is_flag=True, help="Poll until ready or timeout.")
@click.option("--poll-interval", default=10, help="Seconds between polls when --wait.")
@click.option("--timeout", default=900, help="Max seconds to wait when --wait.")
def fetch(project: str, subject: str, variant: str, wait: bool, poll_interval: int, timeout: int) -> None:
    """Download the model for a previously-uploaded task. Only when you say so."""
    require_project(project)
    find_subject(project, subject)
    target = variant_dir(project, subject, variant)
    task_path = target / "task.json"
    if not task_path.exists():
        click.echo(f"error: no task.json in {target}", err=True)
        click.echo(f"hint: pb upload {project} {subject} {variant} --backend <name>", err=True)
        sys.exit(1)

    record = json.loads(task_path.read_text())
    backend_name = record["backend"]
    task_id = record["task_id"]
    try:
        backend = backends.get(backend_name)
    except KeyError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    deadline = time.time() + timeout
    while True:
        try:
            status = backend.status(task_id)
        except Exception as e:
            click.echo(f"error querying status: {e}", err=True)
            sys.exit(1)

        record["last_status"] = status["state"]
        task_path.write_text(json.dumps(record, indent=2) + "\n")

        if status["state"] == "ready":
            try:
                data = backend.fetch(task_id, status["model_urls"] or {})
            except Exception as e:
                click.echo(f"error downloading: {e}", err=True)
                sys.exit(1)
            out = target / "model.stl"
            out.write_bytes(data)
            click.echo(f"✓ saved {out} ({len(data)} bytes)")
            return

        if status["state"] == "failed":
            click.echo(f"task failed: {status.get('error')}", err=True)
            sys.exit(1)

        if not wait:
            click.echo(
                f"task {task_id} is {status['state']}; preview: {record['preview_url']}",
                err=True,
            )
            sys.exit(1)

        if time.time() > deadline:
            click.echo(f"timeout waiting for task {task_id}", err=True)
            sys.exit(1)

        click.echo(f"  status={status['state']} (polling in {poll_interval}s)")
        time.sleep(poll_interval)
```

- [ ] **Step 4: Run tests — pass**

Run: `pytest tests/test_cli_fetch.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add pb/cli.py tests/test_cli_fetch.py
git commit -m "feat: pb fetch — query status, download STL only on ready"
```

---

### Task 12: `pb retry` command

**Files:**
- Modify: `pb/cli.py`
- Test: `tests/test_cli_retry.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_retry.py`:

```python
"""Tests for `pb retry` — archive prior task.json, resubmit with new params/backend."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from pb.cli import cli


@pytest.fixture
def variant_with_old_task(variant):
    (variant / "front.png").write_bytes(b"\x89PNG-front")
    (variant / "back.png").write_bytes(b"\x89PNG-back")
    (variant / "task.json").write_text(json.dumps({
        "backend": "meshy", "task_id": "old-task", "preview_url": "u",
        "submitted_at": "2026-05-14T10:00:00+00:00",
        "params": {}, "views_used": ["front", "back"], "last_status": "failed",
    }))
    return variant


def test_retry_archives_old_task_json_and_writes_new(variant_with_old_task):
    fake_backend = MagicMock()
    fake_backend.submit.return_value = {"task_id": "new-task", "preview_url": "v"}
    with patch("pb.cli.backends.get", return_value=fake_backend):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "retry", "soviets", "at-rifle-team", "v1",
            "--backend", "meshy", "--param", "topology=quad",
        ])
    assert result.exit_code == 0, result.output
    assert (variant_with_old_task / "task.1.json").exists()
    archived = json.loads((variant_with_old_task / "task.1.json").read_text())
    assert archived["task_id"] == "old-task"
    new = json.loads((variant_with_old_task / "task.json").read_text())
    assert new["task_id"] == "new-task"
    assert new["params"] == {"topology": "quad"}


def test_retry_increments_archive_index(variant_with_old_task):
    (variant_with_old_task / "task.1.json").write_text("{}")
    (variant_with_old_task / "task.2.json").write_text("{}")
    fake_backend = MagicMock()
    fake_backend.submit.return_value = {"task_id": "n", "preview_url": "u"}
    with patch("pb.cli.backends.get", return_value=fake_backend):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "retry", "soviets", "at-rifle-team", "v1", "--backend", "meshy",
        ])
    assert result.exit_code == 0
    assert (variant_with_old_task / "task.3.json").exists()


def test_retry_without_existing_task(variant):
    (variant / "front.png").write_bytes(b"\x89PNG")
    fake_backend = MagicMock()
    fake_backend.submit.return_value = {"task_id": "n", "preview_url": "u"}
    with patch("pb.cli.backends.get", return_value=fake_backend):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "retry", "soviets", "at-rifle-team", "v1", "--backend", "meshy",
        ])
    # Retry on a variant with no prior task.json should still work — no archive needed.
    assert result.exit_code == 0
    assert (variant / "task.json").exists()
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_cli_retry.py -v`
Expected: no `retry` command.

- [ ] **Step 3: Implement `pb retry`**

Add to `pb/cli.py`:

```python
import re as _re_for_archive


def _next_archive_index(variant_path: Path) -> int:
    pattern = _re_for_archive.compile(r"^task\.(\d+)\.json$")
    used = {int(m.group(1)) for child in variant_path.iterdir()
            if (m := pattern.match(child.name))}
    n = 1
    while n in used:
        n += 1
    return n


@cli.command()
@click.argument("project")
@click.argument("subject")
@click.argument("variant")
@click.option("--backend", "backend_name", required=True)
@click.option("--param", "params", multiple=True)
def retry(project: str, subject: str, variant: str, backend_name: str, params: tuple[str, ...]) -> None:
    """Archive the prior task.json and resubmit with new backend/params."""
    require_project(project)
    find_subject(project, subject)
    target = variant_dir(project, subject, variant)

    if not target.is_dir():
        click.echo(f"error: variant dir does not exist: {target}", err=True)
        sys.exit(1)

    existing = target / "task.json"
    if existing.exists():
        idx = _next_archive_index(target)
        existing.rename(target / f"task.{idx}.json")
        click.echo(f"archived prior task to task.{idx}.json")

    views = _collect_views(target)
    if not views:
        click.echo(f"error: no labelled views in {target}", err=True)
        sys.exit(1)

    try:
        backend = backends.get(backend_name)
    except KeyError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    parsed_params = _parse_params(params)
    try:
        result = backend.submit(views, parsed_params)
    except (ValueError, RuntimeError) as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    record = {
        "backend": backend_name,
        "task_id": result["task_id"],
        "preview_url": result["preview_url"],
        "submitted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "params": parsed_params,
        "views_used": sorted(set(views) & set(backend.accepted_views)),
        "last_status": "pending",
    }
    existing.write_text(json.dumps(record, indent=2) + "\n")

    click.echo(f"✓ resubmitted task {result['task_id']}")
    click.echo(f"  preview: {result['preview_url']}")
```

- [ ] **Step 4: Run tests — pass**

Run: `pytest tests/test_cli_retry.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run full suite**

Run: `pytest -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add pb/cli.py tests/test_cli_retry.py
git commit -m "feat: pb retry — archive prior task.json, resubmit with new params"
```

---

### Task 13: Update `pb list` state vocabulary

**Files:**
- Modify: `pb/cli.py`
- Test: `tests/test_cli_list.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_list.py`:

```python
"""Tests for `pb list` — state vocabulary across the variant lifecycle."""

from __future__ import annotations

import json

from click.testing import CliRunner

from pb.cli import cli


def test_list_no_subjects_directory(project):
    # Subject has no directory yet → "—"
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "soviets"])
    assert result.exit_code == 0
    assert "—" in result.output


def test_list_state_empty(project):
    (project / "at-rifle-team" / "v1").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "soviets"])
    assert "v1[empty]" in result.output


def test_list_state_cropped(project):
    v = project / "at-rifle-team" / "v1"
    v.mkdir(parents=True)
    (v / "source.png").write_bytes(b"\x89PNG")
    (v / "front.png").write_bytes(b"\x89PNG")
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "soviets"])
    assert "v1[cropped]" in result.output


def test_list_state_mesh_pending(project):
    v = project / "at-rifle-team" / "v1"
    v.mkdir(parents=True)
    (v / "source.png").write_bytes(b"\x89PNG")
    (v / "front.png").write_bytes(b"\x89PNG")
    (v / "task.json").write_text(json.dumps({"last_status": "pending"}))
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "soviets"])
    assert "v1[mesh-pending]" in result.output


def test_list_state_mesh_ready(project):
    v = project / "at-rifle-team" / "v1"
    v.mkdir(parents=True)
    (v / "source.png").write_bytes(b"\x89PNG")
    (v / "task.json").write_text(json.dumps({"last_status": "ready"}))
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "soviets"])
    assert "v1[mesh-ready]" in result.output


def test_list_state_stl(project):
    v = project / "at-rifle-team" / "v1"
    v.mkdir(parents=True)
    (v / "source.png").write_bytes(b"\x89PNG")
    (v / "model.stl").write_bytes(b"stl")
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "soviets"])
    assert "v1[stl]" in result.output
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_cli_list.py -v`
Expected: existing list logic doesn't recognise the new states.

- [ ] **Step 3: Replace `list_cmd` body**

In `pb/cli.py`, replace the existing `list_cmd` implementation with:

```python
def _variant_state(variant_path: Path) -> str:
    if (variant_path / "model.stl").exists():
        return "stl"
    task = variant_path / "task.json"
    if task.exists():
        try:
            data = json.loads(task.read_text())
            last = data.get("last_status", "pending")
        except json.JSONDecodeError:
            last = "pending"
        return "mesh-ready" if last == "ready" else "mesh-pending"
    if (variant_path / "source.png").exists():
        # any other PNG besides source.png?
        for child in variant_path.iterdir():
            if child.suffix.lower() == ".png" and child.name != "source.png":
                return "cropped"
    return "empty"


@cli.command(name="list")
@click.argument("project")
def list_cmd(project: str) -> None:
    """Show subjects and variant states."""
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
                variants.append(f"{v.name}[{_variant_state(v)}]")
        rows.append((name, ", ".join(variants) if variants else "—"))

    width = max(len(r[0]) for r in rows)
    for name, vs in rows:
        click.echo(f"  {name.ljust(width)}  {vs}")
```

- [ ] **Step 4: Run tests — pass**

Run: `pytest tests/test_cli_list.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run full suite**

Run: `pytest -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add pb/cli.py tests/test_cli_list.py
git commit -m "feat: pb list — extended state vocabulary (cropped/mesh-pending/mesh-ready/stl)"
```

---

### Task 14: Remove `pb stage`; replace `pb meshify` with renamed-error stub

**Files:**
- Modify: `pb/cli.py`
- Test: `tests/test_cli_removed_commands.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_removed_commands.py`:

```python
"""Tests that `pb stage` and old `pb meshify` are no longer functional."""

from __future__ import annotations

from click.testing import CliRunner

from pb.cli import cli


def test_stage_command_gone():
    runner = CliRunner()
    result = runner.invoke(cli, ["stage", "soviets", "at-rifle-team", "v1"])
    assert result.exit_code != 0
    assert "no such command" in result.output.lower()


def test_meshify_prints_renamed_message():
    runner = CliRunner()
    result = runner.invoke(cli, ["meshify", "soviets", "at-rifle-team", "v1"])
    assert result.exit_code != 0
    assert "renamed" in result.output.lower()
    assert "upload" in result.output.lower()
    assert "fetch" in result.output.lower()
```

- [ ] **Step 2: Run tests — fail**

Run: `pytest tests/test_cli_removed_commands.py -v`
Expected: stage still exists; meshify still does old behaviour.

- [ ] **Step 3: Delete `pb stage` command**

Remove the entire `@cli.command()` block for `stage` from `pb/cli.py`.

- [ ] **Step 4: Replace `pb meshify` body with renamed-error stub**

Replace the entire existing `meshify` command (and its `--poll-interval` / `--timeout` options) with:

```python
@cli.command()
@click.argument("project", required=False)
@click.argument("subject", required=False)
@click.argument("variant", required=False)
def meshify(project, subject, variant) -> None:
    """Removed — replaced by `pb upload` and `pb fetch`."""
    click.echo(
        "error: `pb meshify` is renamed. Use `pb upload <project> <subject> <variant> --backend meshy`,"
        " then `pb fetch <project> <subject> <variant>`. See README for details.",
        err=True,
    )
    sys.exit(2)
```

- [ ] **Step 5: Remove the now-unused `MESHY_API_BASE` / `MESHY_ENDPOINT` constants from cli.py**

Delete those two constants if still present in cli.py (they live in `pb/backends/meshy.py` now).

Also remove `import base64` and any other imports that are no longer used in cli.py. Check by running `pyflakes pb/cli.py` if available, or visually.

- [ ] **Step 6: Run tests — pass**

Run: `pytest tests/test_cli_removed_commands.py -v`
Expected: 2 passed.

- [ ] **Step 7: Run full suite**

Run: `pytest -v`
Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add pb/cli.py tests/test_cli_removed_commands.py
git commit -m "feat: remove pb stage; replace pb meshify with renamed-error stub"
```

---

### Task 15: Update README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite the README sections that reference the old loop**

Edit `README.md` so the loop and command list match the new behaviour. Replace the `## The loop` section with:

```markdown
## The loop

```bash
pb init soviets
# edit projects/soviets/style.md, drop in seed.png, list subjects in subjects.yaml

pb prompt soviets at-rifle-team
# clipboards the full brief — paste into ChatGPT/Gemini/Claude,
# iterate conversationally, save the chosen final image

pb crop soviets at-rifle-team v1 ~/Downloads/at-team.png
# opens a localhost cropper in your browser
# draw labelled regions (front/back/top/...), click save

pb upload soviets at-rifle-team v1 --backend meshy
# submits the labelled views; prints task ID + preview URL; exits

# ...open the preview URL, look at the mesh, decide...

pb fetch soviets at-rifle-team v1
# downloads model.stl into the variant dir
# (or `pb retry` with different params if the mesh isn't right)

pb learn soviets "Tighter front crop helps Meshy resolve the helmet."
# dated entry appended to style.md
```

Replace the `## What pb deliberately does not do` and `## What it does do` lists to mention multi-backend, judgement-at-the-mesh-layer, and labelled-region capture. Keep the prose concise — under ~30 lines total in those sections.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README — new loop with crop/upload/fetch/retry"
```

---

### Task 16: Final whole-suite verification

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: all tests pass; no warnings about deprecated APIs.

- [ ] **Step 2: Sanity-check pb commands manually**

Run each in turn against a temp directory:

```bash
export PB_ROOT=$(mktemp -d)
pb init demo
# Edit $PB_ROOT/projects/demo/subjects.yaml to add a subject
echo 'subjects:
  - name: thing
    description: |
      A test subject.' > "$PB_ROOT/projects/demo/subjects.yaml"
pb prompt demo thing      # check brief is on clipboard
pb list demo              # should show "thing  —"
pb meshify demo thing v1  # should print renamed-error and exit non-zero
pb stage demo thing v1    # should print "no such command"
pb --help                 # no `stage`, `meshify` shown as removed
```

Expected: each command behaves as planned.

- [ ] **Step 3: Verify the cropper end-to-end manually**

```bash
# Drop any PNG into ~/Downloads/test.png
pb crop demo thing v1 ~/Downloads/test.png
```

Browser opens to `http://127.0.0.1:<port>/`. Draw two rectangles, label them, save. Confirm `$PB_ROOT/projects/demo/thing/v1/{source.png,front.png,back.png,regions.json}` all exist.

- [ ] **Step 4: Commit anything outstanding**

If there are any lint cleanups, README typo fixes, or unused-import removals discovered during the manual run, commit them now:

```bash
git add -A
git status
git commit -m "chore: post-implementation cleanups from manual verification"
```

If there's nothing to commit, skip.

---

## Spec coverage check (self-review)

Each spec section maps to a task:

- **Project tree** (`source.png`, `regions.json`, per-view PNGs, `task.json`, `model.stl`) — Tasks 6, 9, 10, 11, 12 produce/consume these.
- **Command surface** — Task 9 (`crop`), Task 10 (`upload`), Task 11 (`fetch`), Task 12 (`retry`), Task 13 (`list` state), Task 14 (`stage` removed, `meshify` stub).
- **Backend adapter contract** — Task 3 (protocol + registry), Task 4 (Meshy), Task 5 (Hi3D stub).
- **Cropper internals** — Tasks 6 (crop fn), 7 (server), 8 (UI).
- **Error handling** — Spec lists 6 cases:
  - Missing project / subject — covered by `require_project` / `find_subject` (existing pattern, used in Tasks 9–12).
  - Cropper port in use — `port=0` in `start_server` picks a free port (Task 7).
  - Cropper save with zero-area regions — rejected in Task 6's `crop_to_files` and tested.
  - `pb upload` submit failure — caught in Task 10, exits non-zero.
  - `pb fetch` while pending — Task 11 exits non-zero with state + preview URL.
  - Missing API key — `MeshyBackend._api_key` raises with the env var name (Task 4).
- **Testing** — covered task-by-task; Hi3D stub tested in Task 5; cropper save endpoint tested in Task 7; no JS automation (per spec).
- **Migration plan** — Step 1 (extract Meshy) is Task 4; Step 2 (new commands) Tasks 9–12; Step 3 (remove stage) Task 14; Step 4 (extend list) Task 13; Step 5 (Hi3D stub) Task 5.

No gaps. No placeholders. Type names (`SubmitResult`, `StatusResult`, `MeshBackend`) consistent across Tasks 3, 4, 5, 10, 11, 12.
