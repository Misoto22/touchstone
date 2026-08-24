from __future__ import annotations

import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_metadata_declares_public_contract() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]

    assert project["name"] == "touchstone-agent"
    assert project["license"] == "Apache-2.0"
    assert project["readme"] == "README.md"
    assert "dev" in project["optional-dependencies"]


def test_release_uses_pypi_trusted_publishing_only() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "release:" in workflow and "published" in workflow
    assert "id-token: write" in workflow
    assert "environment:" in workflow and "name: pypi" in workflow
    assert "password:" not in workflow and "PYPI_API_TOKEN" not in workflow


def test_built_wheel_runs_without_the_source_checkout(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("touchstone_agent-*.whl"))
    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )

    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(environment / "bin" / "touchstone"), "graph"],
        cwd=tmp_path,
        env=clean_env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "audit" in result.stdout
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    assert "touchstone/resources/briefs/code-audit.md" in names
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
