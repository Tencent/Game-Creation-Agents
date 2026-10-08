from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).parents[4]


def _load_preflight_module():
    module_path = REPO_ROOT / "scripts" / "preflight_env.py"
    spec = importlib.util.spec_from_file_location("gca_preflight_env", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PREFLIGHT = _load_preflight_module()


def _ok_result(name: str, category: str = "system"):
    return PREFLIGHT.CheckResult(
        name=name,
        category=category,
        ok=True,
        severity="info",
        message=name,
    )


def test_repository_host_requirements_are_explicit_and_match_readme() -> None:
    requirements = PREFLIGHT._load_preflight_requirements(
        REPO_ROOT / "scripts" / "preflight-requirements.toml"
    )

    assert requirements["node_minimum_major"] == 20
    binaries = {
        binary
        for dependency in requirements["system_dependencies"]
        for binary in dependency["binaries"]
    }
    assert binaries == {"git", "curl", "ffmpeg", "ffprobe", "uv"}

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    retired_container_file = "Dockerfile" + ".sandbox"
    assert "Node.js 20 or newer" in readme
    assert "](scripts/preflight-requirements.toml)" in readme
    assert retired_container_file not in readme
    assert not (REPO_ROOT / retired_container_file).exists()


def test_repository_matting_backend_is_auto() -> None:
    assert PREFLIGHT._parse_matting_backend(REPO_ROOT / "gca.config.yaml") == "auto"


@pytest.mark.parametrize(
    ("backend", "severity"),
    [("auto", "warning"), ("rmbg2", "blocker")],
)
def test_rmbg_network_502_respects_matting_policy(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    severity: str,
) -> None:
    monkeypatch.setenv(
        "RMBG_BASE_URL",
        "http://rmbg.example/v1/images/remove-background",
    )
    monkeypatch.setitem(
        sys.modules,
        "requests",
        SimpleNamespace(
            head=lambda *_args, **_kwargs: SimpleNamespace(status_code=502)
        ),
    )

    result = PREFLIGHT.check_network_rmbg(matting_backend=backend)

    assert not result.ok
    assert result.severity == severity


def test_explicit_color_key_skips_remote_rmbg_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RMBG_BASE_URL", raising=False)

    result = PREFLIGHT.check_network_rmbg(matting_backend="color_key")

    assert result.ok
    assert result.severity == "info"


def test_seedance_frames_do_not_require_object_storage() -> None:
    result = PREFLIGHT.check_upload_backend([])

    assert result.ok
    assert result.severity == "info"
    assert "object storage is not required" in result.message


def test_requirements_loader_rejects_duplicate_binaries(tmp_path: Path) -> None:
    config = tmp_path / "preflight-requirements.toml"
    config.write_text(
        """\
schema_version = 1
[node]
minimum_major = 20
[[system_dependencies]]
name = "one"
binaries = ["shared"]
[[system_dependencies]]
name = "two"
binaries = ["shared"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate binary: shared"):
        PREFLIGHT._load_preflight_requirements(config)


def test_system_binary_check_uses_dependency_package_for_all_binaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirements = PREFLIGHT._load_preflight_requirements(
        REPO_ROOT / "scripts" / "preflight-requirements.toml"
    )
    available = {"git", "curl", "ffmpeg", "uv"}
    monkeypatch.setattr(
        PREFLIGHT.shutil,
        "which",
        lambda binary: f"/usr/bin/{binary}" if binary in available else None,
    )
    monkeypatch.setattr(PREFLIGHT, "IS_MACOS", True)
    monkeypatch.setattr(PREFLIGHT, "IS_LINUX", False)

    results = {
        result.name: result
        for result in PREFLIGHT.check_system_binaries(
            requirements["system_dependencies"]
        )
    }

    assert results["binary_ffmpeg"].ok
    assert not results["binary_ffprobe"].ok
    assert results["binary_ffprobe"].fix_cmd == "brew install ffmpeg"
    assert results["binary_ffprobe"].severity == "blocker"


def test_node_fix_command_uses_configured_major(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PREFLIGHT.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(PREFLIGHT, "IS_MACOS", False)
    monkeypatch.setattr(PREFLIGHT, "IS_LINUX", True)
    monkeypatch.setattr(PREFLIGHT, "_LINUX_PKG_MGR", "apt-get")

    result = PREFLIGHT.check_node_version(22)

    assert not result.ok
    assert "setup_22.x" in result.fix_cmd


def test_missing_requirements_file_blocks_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        PREFLIGHT,
        "PREFLIGHT_REQUIREMENTS",
        tmp_path / "missing-requirements.toml",
    )
    monkeypatch.setattr(PREFLIGHT, "check_mcp_server_boots", lambda: _ok_result("mcp"))
    monkeypatch.setattr(PREFLIGHT, "check_python_packages", lambda _deps: [])
    monkeypatch.setattr(PREFLIGHT, "check_playwright", lambda: _ok_result("playwright", "node"))
    monkeypatch.setattr(PREFLIGHT, "check_env_vars", lambda _entries: [])
    monkeypatch.setattr(PREFLIGHT, "check_upload_backend", lambda _entries: _ok_result("upload"))
    monkeypatch.setattr(PREFLIGHT, "check_filesystem", lambda: _ok_result("filesystem", "filesystem"))
    monkeypatch.setattr(PREFLIGHT, "check_runner_scripts", lambda _scripts: [])

    report = PREFLIGHT.run_preflight(quick=True)

    requirement_results = [
        result for result in report.results if result.name == "preflight_requirements"
    ]
    assert len(requirement_results) == 1
    assert not requirement_results[0].ok
    assert requirement_results[0].severity == "blocker"
    assert not report.all_pass
