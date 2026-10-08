#!/usr/bin/env python3
"""GCA Environment Preflight Check — run before any pipeline execution.

All check lists are DERIVED from project source files (single source of truth):
  - Python deps     ← gca-mcp/pyproject.toml [project.dependencies]
  - Env vars        ← .env.example (uncommented KEY=... lines)
  - System binaries ← scripts/preflight-requirements.toml
  - Node.js version ← scripts/preflight-requirements.toml
  - Runner scripts  ← gca-mcp/src/gca_mcp/runners/**/*.{py,mjs,js} (glob)
  - Node packages   ← runner scripts' `import '...'` / `require('...')` (scan)

Adding a new dependency to any of those files automatically makes preflight
check for it — no manual sync needed.

Usage:
    python3 scripts/preflight_env.py                    # full check
    python3 scripts/preflight_env.py --quick            # skip network probes
    python3 scripts/preflight_env.py --json             # machine-readable output
    python3 scripts/preflight_env.py --fix              # fix ALL blockers (incl. system pkgs)
    python3 scripts/preflight_env.py --auto-fix         # fix safe blockers only (pipeline mode)
    python3 scripts/preflight_env.py --quick --auto-fix # pipeline startup: quick + safe repair

Exit codes: 0 = pass, 1 = blockers, 2 = warnings only

Auto-fix safety tiers:
  Safe (--auto-fix): uv sync, npm install, npx playwright install
  System (--fix only): apt-get/dnf/yum install, curl|bash scripts
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ─── Paths ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
GCA_MCP_DIR = REPO_ROOT / "gca-mcp"
RUNNERS_ROOT = GCA_MCP_DIR / "src" / "gca_mcp" / "runners"
PYPROJECT = GCA_MCP_DIR / "pyproject.toml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_FILE = REPO_ROOT / ".env"
PREFLIGHT_REQUIREMENTS = REPO_ROOT / "scripts" / "preflight-requirements.toml"
RUNTIME_CONFIG = REPO_ROOT / "gca.config.yaml"

IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"


# ─── Source Parsers (derive check lists from project files) ───────────────────


def _parse_pyproject_deps(path: Path) -> list[dict[str, str]]:
    """Extract dependencies from pyproject.toml → [{name, import_name, spec}]."""
    # Known PyPI name → import name mappings (only for non-trivial cases)
    IMPORT_MAP = {
        "pillow": "PIL",
    }

    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    # Find [project] dependencies array
    m = re.search(r'dependencies\s*=\s*\[(.*?)\]', text, re.DOTALL)
    if not m:
        return []
    deps = []
    for line in m.group(1).splitlines():
        line = line.strip().strip('",').strip("',")
        if not line or line.startswith("#"):
            continue
        # "mcp>=1.2.0" → name="mcp", spec=">=1.2.0"
        parts = re.split(r'[><=!~;]', line, maxsplit=1)
        name = parts[0].strip().lower()
        import_name = IMPORT_MAP.get(name, name.replace("-", "_"))
        deps.append({"name": name, "import_name": import_name, "spec": line})
    return deps


def _parse_python_version_requirement(path: Path) -> tuple[int, int] | None:
    """Extract requires-python from pyproject.toml."""
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _parse_env_example(path: Path) -> list[dict[str, Any]]:
    """Parse .env.example → [{key, required, has_placeholder, section}].

    Rules:
      - Lines starting with # are comments
      - Uncommented KEY=value lines are REQUIRED
      - Uncommented KEY= with an empty value is OPTIONAL: the template declares
        the key so that a parent directory's .env cannot fill it in, which is
        a deliberate blank rather than a value the user still owes us
      - Commented-out # KEY=value lines (with a value) are OPTIONAL
      - Value containing <...> is a placeholder (user must fill)
    """
    if not path.is_file():
        return []
    entries = []
    current_section = "general"
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        # Section headers (lines like "# === Section Name ===")
        if stripped.startswith("#") and "===" in stripped:
            current_section = stripped.strip("# =").strip()
            continue
        # Commented-out variable (optional)
        if stripped.startswith("# ") and "=" in stripped and not stripped.startswith("# -"):
            key = stripped[2:].partition("=")[0].strip()
            if key.isupper() or key.upper() == key:
                entries.append({"key": key, "required": False, "section": current_section})
            continue
        # Active variable (required)
        if not stripped.startswith("#") and "=" in stripped and stripped:
            key, _, value = stripped.partition("=")
            key = key.strip()
            if not key or not key[0].isalpha():
                continue
            has_placeholder = "<" in value and ">" in value
            entries.append({
                "key": key,
                "required": bool(value.strip()),
                "has_placeholder": has_placeholder,
                "section": current_section,
            })
    return entries


def _load_preflight_requirements(path: Path) -> dict[str, Any]:
    """Load and validate host runtime requirements from TOML."""
    if not path.is_file():
        raise ValueError(f"Preflight requirements file not found: {path}")

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid preflight requirements TOML: {exc}") from exc

    if data.get("schema_version") != 1:
        raise ValueError("preflight requirements schema_version must be 1")

    node = data.get("node")
    minimum_major = node.get("minimum_major") if isinstance(node, dict) else None
    if (
        not isinstance(minimum_major, int)
        or isinstance(minimum_major, bool)
        or minimum_major < 1
    ):
        raise ValueError("preflight requirements node.minimum_major must be a positive integer")

    raw_dependencies = data.get("system_dependencies")
    if not isinstance(raw_dependencies, list) or not raw_dependencies:
        raise ValueError("preflight requirements system_dependencies must be a non-empty array")

    dependencies: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_binaries: set[str] = set()
    for index, raw in enumerate(raw_dependencies):
        label = f"system_dependencies[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"preflight requirements {label} must be a table")

        name = raw.get("name")
        binaries = raw.get("binaries")
        severity = raw.get("severity", "blocker")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"preflight requirements {label}.name must be non-empty")
        name = name.strip()
        if name in seen_names:
            raise ValueError(f"preflight requirements duplicate dependency name: {name}")
        if (
            not isinstance(binaries, list)
            or not binaries
            or any(not isinstance(item, str) or not item.strip() for item in binaries)
        ):
            raise ValueError(f"preflight requirements {label}.binaries must be non-empty strings")
        if severity not in {"blocker", "warning"}:
            raise ValueError(f"preflight requirements {label}.severity must be blocker or warning")

        normalized_binaries = [item.strip() for item in binaries]
        if len(set(normalized_binaries)) != len(normalized_binaries):
            raise ValueError(f"preflight requirements duplicate binary in {label}")
        duplicates = seen_binaries.intersection(normalized_binaries)
        if duplicates:
            duplicate = sorted(duplicates)[0]
            raise ValueError(f"preflight requirements duplicate binary: {duplicate}")

        normalized: dict[str, Any] = {
            "name": name,
            "binaries": normalized_binaries,
            "severity": severity,
        }
        for key in ("macos_package", "linux_package", "hint"):
            value = raw.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"preflight requirements {label}.{key} must be non-empty")
            if isinstance(value, str):
                normalized[key] = value.strip()

        dependencies.append(normalized)
        seen_names.add(name)
        seen_binaries.update(normalized_binaries)

    return {
        "node_minimum_major": minimum_major,
        "system_dependencies": dependencies,
    }


def _parse_matting_backend(path: Path) -> str:
    """Read assets.matting.backend without requiring a YAML dependency."""
    if not path.is_file():
        return "rmbg2"
    top_level = ""
    subsection = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        content = raw_line.split("#", 1)[0].rstrip()
        if not content.strip() or ":" not in content:
            continue
        indent = len(content) - len(content.lstrip())
        key, _, value = content.strip().partition(":")
        value = value.strip().strip("'\"")
        if indent == 0:
            top_level = key
            subsection = ""
        elif indent == 2 and top_level == "assets":
            subsection = key
        elif (
            indent == 4
            and top_level == "assets"
            and subsection == "matting"
            and key == "backend"
        ):
            return value if value in {"auto", "rmbg2", "color_key"} else "rmbg2"
    return "rmbg2"


def _discover_runner_scripts(root: Path) -> list[Path]:
    """Glob all executable runner scripts (skip __init__.py)."""
    if not root.is_dir():
        return []
    scripts = []
    for ext in ("*.py", "*.mjs", "*.js"):
        for p in root.rglob(ext):
            if p.name == "__init__.py":
                continue
            scripts.append(p)
    return sorted(scripts)


# ─── Check Result Types ───────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    category: str
    ok: bool
    severity: str = "blocker"
    message: str = ""
    hint: str = ""
    fix_cmd: str = ""


@dataclass
class PreflightReport:
    generated_at: str = ""
    duration_ms: int = 0
    results: list[CheckResult] = field(default_factory=list)

    @property
    def blockers(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and r.severity == "blocker"]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and r.severity == "warning"]

    @property
    def all_pass(self) -> bool:
        return len(self.blockers) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "duration_ms": self.duration_ms,
            "ok": self.all_pass,
            "total_checks": len(self.results),
            "passed": sum(1 for r in self.results if r.ok),
            "blockers": len(self.blockers),
            "warnings": len(self.warnings),
            "results": [
                {
                    "name": r.name,
                    "category": r.category,
                    "ok": r.ok,
                    "severity": r.severity,
                    "message": r.message,
                    **({"hint": r.hint} if r.hint else {}),
                    **({"fix_cmd": r.fix_cmd} if r.fix_cmd else {}),
                }
                for r in self.results
            ],
        }


# ─── Check Functions ──────────────────────────────────────────────────────────


def _detect_linux_pkg_manager() -> str:
    """Detect the system package manager on Linux."""
    for mgr in ("dnf", "yum", "apt-get"):
        if shutil.which(mgr):
            return mgr
    return "apt-get"


_LINUX_PKG_MGR: str | None = None


def _linux_pkg_mgr() -> str:
    global _LINUX_PKG_MGR
    if _LINUX_PKG_MGR is None:
        _LINUX_PKG_MGR = _detect_linux_pkg_manager()
    return _LINUX_PKG_MGR


def _fix_cmd_for_dependency(dependency: dict[str, Any]) -> str:
    if IS_MACOS:
        package = dependency.get("macos_package")
        return f"brew install {package}" if package else ""
    if IS_LINUX:
        package = dependency.get("linux_package")
        if not package:
            return ""
        mgr = _linux_pkg_mgr()
        if mgr in ("dnf", "yum"):
            return f"{mgr} install -y {package}"
        return f"apt-get install -y {package}"
    return ""


def check_python_version(required: tuple[int, int]) -> CheckResult:
    v = sys.version_info
    ok = (v.major, v.minor) >= required
    return CheckResult(
        name="python_version",
        category="system",
        ok=ok,
        message=f"Python {v.major}.{v.minor}.{v.micro}" + (f" (need ≥{required[0]}.{required[1]})" if not ok else ""),
        hint="" if ok else f"Install Python {required[0]}.{required[1]}+",
    )


def _fix_cmd_for_node(required_major: int) -> str:
    """Platform-aware Node.js install command."""
    if IS_MACOS:
        return "brew install node"
    if IS_LINUX:
        mgr = _linux_pkg_mgr()
        if mgr in ("dnf", "yum"):
            return (
                f"curl -fsSL https://rpm.nodesource.com/setup_{required_major}.x | "
                f"bash - && {mgr} install -y nodejs"
            )
        return (
            f"curl -fsSL https://deb.nodesource.com/setup_{required_major}.x | "
            "bash - && apt-get install -y nodejs"
        )
    return ""


def check_node_version(required_major: int) -> CheckResult:
    node = shutil.which("node")
    if not node:
        return CheckResult(
            name="node_version", category="system", ok=False,
            message="node not found on PATH",
            hint=f"Install Node.js ≥{required_major}",
            fix_cmd=_fix_cmd_for_node(required_major),
        )
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        major = int(out.lstrip("v").split(".")[0])
        ok = major >= required_major
        return CheckResult(
            name="node_version", category="system", ok=ok,
            message=f"Node.js {out}" + ("" if ok else f" (need ≥{required_major})"),
            hint="" if ok else f"Upgrade Node.js to ≥{required_major}",
        )
    except Exception as e:
        return CheckResult(name="node_version", category="system", ok=False, message=str(e))


def check_system_binaries(
    dependencies: list[dict[str, Any]],
) -> list[CheckResult]:
    """Check every host binary declared by the preflight requirements file."""
    results = []
    for dependency in dependencies:
        fix_cmd = _fix_cmd_for_dependency(dependency)
        for binary in dependency["binaries"]:
            path = shutil.which(binary)
            results.append(CheckResult(
                name=f"binary_{binary}", category="system",
                ok=path is not None,
                severity=dependency["severity"],
                message=f"{binary}: {path}" if path else f"{binary}: NOT FOUND",
                hint="" if path else dependency.get("hint", f"Install {dependency['name']}"),
                fix_cmd="" if path else fix_cmd,
            ))
    return results


def check_python_packages(deps: list[dict[str, str]]) -> list[CheckResult]:
    """Check packages inside gca-mcp's venv."""
    venv_python = GCA_MCP_DIR / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        # Try Windows path
        venv_python = GCA_MCP_DIR / ".venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        return [CheckResult(
            name="gca_mcp_venv", category="python", ok=False,
            message="gca-mcp venv not found",
            hint="Run: cd gca-mcp && uv sync",
            fix_cmd=f"cd {GCA_MCP_DIR} && uv sync",
        )]

    # Build a single script that checks all imports
    import_checks = []
    for dep in deps:
        import_checks.append(
            f'try:\n __import__("{dep["import_name"]}")\n print("{dep["name"]}:ok")\n'
            f'except ImportError:\n print("{dep["name"]}:missing")'
        )
    check_script = "\n".join(import_checks)

    try:
        proc = subprocess.run(
            [str(venv_python), "-c", check_script],
            capture_output=True, text=True, timeout=15, check=False,
        )
        results = []
        found = set()
        for line in proc.stdout.strip().splitlines():
            if ":" not in line:
                continue
            pkg, status = line.rsplit(":", 1)
            found.add(pkg)
            ok = status == "ok"
            results.append(CheckResult(
                name=f"pkg_{pkg}", category="python", ok=ok,
                message=f"{pkg}: {'ok' if ok else 'NOT importable'} (venv)",
                hint="" if ok else "Run: cd gca-mcp && uv sync",
                fix_cmd="" if ok else f"cd {GCA_MCP_DIR} && uv sync",
            ))
        for dep in deps:
            if dep["name"] not in found:
                results.append(CheckResult(
                    name=f"pkg_{dep['name']}", category="python", ok=False,
                    message=f"{dep['name']}: check failed",
                    fix_cmd=f"cd {GCA_MCP_DIR} && uv sync",
                ))
        return results
    except Exception as e:
        return [CheckResult(
            name="python_packages", category="python", ok=False,
            message=f"Package check failed: {e}",
            fix_cmd=f"cd {GCA_MCP_DIR} && uv sync",
        )]


def check_playwright() -> CheckResult:
    # Mirror the bundled verify scripts' EXACT loader (runners/verify/*.mjs):
    # ESM `import('playwright')` resolved from the runners/verify module location,
    # with the same `npm root -g` fallback. Running with cwd=runners/verify makes
    # the bare-specifier resolution base identical to the real scripts', so this
    # check can't false-positive by probing an unrelated cwd. A game project never
    # depends on playwright; only these runner scripts do.
    script = """
import { execSync } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs';
if (!process.env.NODE_PATH) {
  try {
    const globalRoot = execSync('npm root -g', { encoding: 'utf8' }).trim();
    process.env.NODE_PATH = globalRoot;
    const Module = await import('node:module');
    if (Module.default?._initPaths) Module.default._initPaths();
  } catch {}
}
let chromium;
try {
  ({ chromium } = await import('playwright'));
} catch {
  const require = createRequire(import.meta.url);
  const globalRoot = execSync('npm root -g', { encoding: 'utf8' }).trim();
  ({ chromium } = require(`${globalRoot}/playwright`));
}
const p = chromium.executablePath();
if (!fs.existsSync(p)) { console.error('browser_missing:' + p); process.exit(2); }
console.log('ok:' + p);
"""
    verify_dir = RUNNERS_ROOT / "verify"
    try:
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=str(verify_dir),
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode == 0:
            return CheckResult(
                name="playwright", category="node", ok=True,
                message=f"Playwright + Chromium: {proc.stdout.strip()}",
            )
        msg = proc.stderr.strip().split("\n")[0][:200]
        is_browser_missing = proc.returncode == 2
        return CheckResult(
            name="playwright", category="node", ok=False,
            message=f"Playwright: {msg}",
            hint="npx playwright install chromium" if is_browser_missing else "npm install -g playwright && npx playwright install chromium",
            fix_cmd="npx playwright install chromium" if is_browser_missing else "npm install -g playwright && npx playwright install chromium",
        )
    except Exception as e:
        return CheckResult(name="playwright", category="node", ok=False, message=str(e))


def load_env_file() -> None:
    """Merge ``.env`` into the process environment.

    Real shell exports keep precedence via ``setdefault``, and repeated calls
    are harmless.
    """
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def check_env_vars(env_entries: list[dict[str, Any]]) -> list[CheckResult]:
    """Check environment variables based on parsed .env.example."""
    load_env_file()

    results = []
    for entry in env_entries:
        if not entry.get("required"):
            continue
        key = entry["key"]
        val = os.environ.get(key)
        ok = bool(val) and not val.startswith("<")
        results.append(CheckResult(
            name=f"env_{key}", category="credentials",
            ok=ok,
            severity="blocker" if entry.get("has_placeholder") else "warning",
            message=f"{key}: {'set' if ok else 'MISSING or placeholder'}",
            hint="" if ok else f"Set {key} in .env",
        ))
    return results


def check_upload_backend(env_entries: list[dict[str, Any]]) -> CheckResult:
    """Report the built-in inline transport for local Seedance frames."""
    return CheckResult(
        name="upload_backend",
        category="credentials",
        ok=True,
        severity="info",
        message="Seedance frames use inline data URIs; object storage is not required",
    )


def check_runner_scripts(scripts: list[Path]) -> list[CheckResult]:
    """Verify each runner script is syntactically valid."""
    results = []
    for script in scripts:
        rel = script.relative_to(RUNNERS_ROOT)
        if script.suffix in (".mjs", ".js"):
            proc = subprocess.run(
                ["node", "--check", str(script)],
                capture_output=True, text=True, timeout=10, check=False,
            )
        elif script.suffix == ".py":
            proc = subprocess.run(
                [sys.executable, "-m", "py_compile", str(script)],
                capture_output=True, text=True, timeout=10, check=False,
            )
        else:
            continue
        ok = proc.returncode == 0
        results.append(CheckResult(
            name=f"runners/{rel}", category="filesystem",
            ok=ok,
            severity="blocker" if not ok else "info",
            message=f"runners/{rel}: {'ok' if ok else 'SYNTAX ERROR: ' + proc.stderr[:80]}",
        ))
    return results


def check_filesystem() -> CheckResult:
    test_dir = REPO_ROOT / "playground" / ".preflight-probe"
    try:
        test_dir.mkdir(parents=True, exist_ok=True)
        probe = test_dir / "probe.txt"
        probe.write_text("ok")
        assert probe.read_text() == "ok"
        probe.unlink()
        test_dir.rmdir()
        return CheckResult(
            name="filesystem", category="filesystem", ok=True,
            message="playground/ read/write/delete: ok",
        )
    except Exception as e:
        return CheckResult(
            name="filesystem", category="filesystem", ok=False,
            message=f"Filesystem access failed: {e}",
        )


def check_mcp_server_boots() -> CheckResult:
    try:
        proc = subprocess.run(
            ["uv", "--directory", str(GCA_MCP_DIR), "run", "gca-mcp", "--help"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        ok = proc.returncode == 0
        return CheckResult(
            name="mcp_server", category="system", ok=ok,
            message="gca-mcp boots: ok" if ok else f"gca-mcp boot failed: {proc.stderr[:150]}",
            hint="" if ok else "Run: cd gca-mcp && uv sync",
            fix_cmd="" if ok else f"cd {GCA_MCP_DIR} && uv sync",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(name="mcp_server", category="system", ok=False, message="gca-mcp boot timed out")
    except Exception as e:
        return CheckResult(name="mcp_server", category="system", ok=False, message=str(e))


def check_provider_endpoint() -> CheckResult:
    """Validate the configured ARK Seedance task endpoint."""
    endpoint = (os.environ.get("SEEDANCE_URL") or "").strip()
    if endpoint and not endpoint.endswith(
        "/api/v3/contents/generations/tasks"
    ):
        return CheckResult(
            name="provider_endpoint",
            category="config",
            ok=False,
            message="Seedance requires the ARK native task endpoint",
            hint=(
                "Set SEEDANCE_URL to the endpoint ending in "
                "/api/v3/contents/generations/tasks."
            ),
        )
    return CheckResult(
        name="provider_endpoint",
        category="config",
        ok=True,
        message="External provider endpoints selected",
    )


def check_network_rmbg(
    timeout: int = 5,
    *,
    matting_backend: str | None = None,
) -> CheckResult:
    backend = matting_backend or _parse_matting_backend(RUNTIME_CONFIG)
    if backend == "color_key":
        return CheckResult(
            name="network_rmbg",
            category="network",
            ok=True,
            severity="info",
            message="RMBG probe skipped; color_key is the explicit backend",
        )

    endpoint = (os.environ.get("RMBG_BASE_URL") or "").strip()
    if not endpoint:
        return CheckResult(
            name="network_rmbg",
            category="network",
            ok=True,
            severity="info",
            message=(
                "No remote RMBG endpoint configured; MCP preflight will validate "
                f"the {'local/color-key fallback' if backend == 'auto' else 'local backend'}"
            ),
        )

    try:
        import requests as req
        # Try a lightweight probe (HEAD on base path)
        base = endpoint.rsplit("/v1/", 1)[0] if "/v1/" in endpoint else endpoint
        resp = req.head(base, timeout=timeout, allow_redirects=True)
        ok = resp.status_code < 500
        return CheckResult(
            name="network_rmbg", category="network", ok=ok,
            severity="warning" if backend == "auto" and not ok else "blocker",
            message=f"RMBG ({base}): {resp.status_code}",
            hint=(
                ""
                if ok
                else (
                    "Color-key fallback will be validated by MCP preflight"
                    if backend == "auto"
                    else "Start RMBG service or check RMBG_BASE_URL"
                )
            ),
        )
    except Exception as e:
        return CheckResult(
            name="network_rmbg", category="network", ok=False,
            severity="warning" if backend == "auto" else "blocker",
            message=f"RMBG unreachable: {type(e).__name__}",
            hint=(
                "Color-key fallback will be validated by MCP preflight"
                if backend == "auto"
                else "Start RMBG service or check RMBG_BASE_URL"
            ),
        )


def check_network_provider(timeout: int = 5) -> CheckResult:
    url = os.environ.get("GAME_ASSET_BASE_URL", "https://api.openai.com/v1")
    try:
        import requests as req
        req.options(url, timeout=timeout)
        return CheckResult(
            name="network_provider", category="network", ok=True,
            message=f"Provider API ({url.split('/')[2]}): reachable",
        )
    except ImportError:
        # The probe needs `requests`, which lives in the gca-mcp venv rather
        # than the interpreter running this script. Reporting that as an
        # unreachable host sends people chasing a network fault that is not
        # there, so name the real cause instead.
        return CheckResult(
            name="network_provider", category="network", ok=True,
            severity="warning",
            message=(
                f"Provider API ({url.split('/')[2]}): probe skipped, "
                "`requests` unavailable to this interpreter"
            ),
            hint="Run via the gca-mcp venv to enable the probe: cd gca-mcp && uv run python ../scripts/preflight_env.py",
        )
    except Exception as e:
        return CheckResult(
            name="network_provider", category="network", ok=False,
            message=f"Provider API unreachable ({url.split('/')[2]}): {type(e).__name__}",
            hint="Check network access and GAME_ASSET_BASE_URL",
        )


# ─── Main Runner ──────────────────────────────────────────────────────────────


def run_preflight(*, quick: bool = False) -> PreflightReport:
    start = time.time()
    report = PreflightReport()

    # Load .env before any check reads provider configuration.
    load_env_file()

    # ── Derive check lists from project files ──
    python_deps = _parse_pyproject_deps(PYPROJECT)
    python_version_req = _parse_python_version_requirement(PYPROJECT) or (3, 11)
    env_entries = _parse_env_example(ENV_EXAMPLE)
    runner_scripts = _discover_runner_scripts(RUNNERS_ROOT)
    try:
        host_requirements = _load_preflight_requirements(PREFLIGHT_REQUIREMENTS)
    except (OSError, ValueError) as exc:
        host_requirements = None
        report.results.append(CheckResult(
            name="preflight_requirements",
            category="system",
            ok=False,
            severity="blocker",
            message=f"Host requirements config invalid: {exc}",
            hint=f"Repair {PREFLIGHT_REQUIREMENTS}",
        ))

    # ── 1. System ──
    report.results.append(check_provider_endpoint())
    report.results.append(check_python_version(python_version_req))
    if host_requirements is not None:
        report.results.append(check_node_version(host_requirements["node_minimum_major"]))
        report.results.extend(check_system_binaries(host_requirements["system_dependencies"]))
    report.results.append(check_mcp_server_boots())

    # ── 2. Python packages ──
    report.results.extend(check_python_packages(python_deps))

    # ── 3. Node packages ──
    report.results.append(check_playwright())

    # ── 4. Credentials ──
    report.results.extend(check_env_vars(env_entries))
    report.results.append(check_upload_backend(env_entries))

    # ── 5. Filesystem + runner scripts ──
    report.results.append(check_filesystem())
    report.results.extend(check_runner_scripts(runner_scripts))

    # ── 6. Network (skip in --quick) ──
    if not quick:
        report.results.append(
            check_network_rmbg(
                matting_backend=_parse_matting_backend(RUNTIME_CONFIG)
            )
        )
        report.results.append(check_network_provider())

    report.generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report.duration_ms = int((time.time() - start) * 1000)
    return report


# ─── Output ───────────────────────────────────────────────────────────────────


def print_report(report: PreflightReport, *, json_mode: bool = False) -> None:
    if json_mode:
        print(json.dumps(report.to_dict(), indent=2))
        return

    PASS = "\033[32m✓\033[0m"
    FAIL = "\033[31m✗\033[0m"
    WARN = "\033[33m!\033[0m"

    categories = {}
    for r in report.results:
        categories.setdefault(r.category, []).append(r)

    labels = {
        "system": "System & Runtime",
        "python": "Python Packages (gca-mcp venv)",
        "node": "Node.js Packages",
        "credentials": "Credentials (.env)",
        "filesystem": "Filesystem & Vendor Scripts",
        "network": "Network Services",
    }

    for cat, results in categories.items():
        # Skip info-only categories where everything passes
        if all(r.ok for r in results) and all(r.severity == "info" for r in results if not r.ok):
            pass  # still show them for completeness
        print(f"\n{'─' * 60}")
        print(f"  {labels.get(cat, cat)}")
        print(f"{'─' * 60}")
        for r in results:
            if r.severity == "info" and r.ok:
                continue  # hide passing info-level checks
            icon = PASS if r.ok else (WARN if r.severity == "warning" else FAIL)
            print(f"  {icon} {r.message}")
            if not r.ok and r.hint:
                print(f"      → {r.hint}")

    print(f"\n{'═' * 60}")
    total = len(report.results)
    passed = sum(1 for r in report.results if r.ok)
    print(f"  {passed}/{total} passed | "
          f"{len(report.blockers)} blockers | "
          f"{len(report.warnings)} warnings | "
          f"{report.duration_ms}ms")

    if report.all_pass:
        print("  \033[32m✓ Environment ready.\033[0m")
    else:
        print(f"  \033[31m✗ {len(report.blockers)} blocker(s) — fix before running pipeline.\033[0m")
        fix_cmds = [r.fix_cmd for r in report.blockers if r.fix_cmd]
        if fix_cmds:
            print("\n  Fix commands (or run with --fix):")
            for cmd in dict.fromkeys(fix_cmds):  # deduplicate preserving order
                print(f"    $ {cmd}")
    print()


def _is_safe_fix(cmd: str) -> bool:
    """Determine if a fix command is safe to run without sudo/user confirmation.

    Safe: uv sync, npm install, npx playwright install, pip install
    Unsafe: system package install (apt/dnf/yum), curl|bash, rm, chmod
    """
    safe_prefixes = (
        "cd ",       # cd + uv sync pattern
        "uv sync",
        "npm install",
        "npx playwright",
    )
    unsafe_markers = ("apt-get", "dnf ", "yum ", "sudo", "curl", "rm ", "chmod")
    cmd_lower = cmd.lower()
    if any(m in cmd_lower for m in unsafe_markers):
        return False
    # "cd /path && uv sync" is safe
    if "&&" in cmd:
        parts = [p.strip() for p in cmd.split("&&")]
        return all(_is_safe_fix(p) for p in parts if not p.startswith("cd "))
    return any(cmd_lower.startswith(p) for p in safe_prefixes)


def attempt_fixes(report: PreflightReport, *, safe_only: bool = False) -> tuple[int, int]:
    """Run fix commands for failed blockers. Returns (succeeded, failed) counts."""
    fix_cmds = list(dict.fromkeys(r.fix_cmd for r in report.blockers if r.fix_cmd))
    if not fix_cmds:
        print("No auto-fixable blockers.\n")
        return (0, 0)

    if safe_only:
        fix_cmds = [cmd for cmd in fix_cmds if _is_safe_fix(cmd)]
        if not fix_cmds:
            print("No safe auto-fixable blockers (remaining fixes need system privileges).\n")
            return (0, 0)

    print(f"Attempting {len(fix_cmds)} fix(es){' (safe only)' if safe_only else ''}...\n")
    succeeded, failed = 0, 0
    for cmd in fix_cmds:
        safe_tag = " [safe]" if _is_safe_fix(cmd) else " [system]"
        print(f"  $ {cmd}{safe_tag}")
        try:
            proc = subprocess.run(cmd, shell=True, timeout=300, capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                print("    → ok\n")
                succeeded += 1
            else:
                print(f"    → FAILED (exit {proc.returncode})")
                if proc.stderr:
                    for line in proc.stderr.strip().splitlines()[-3:]:
                        print(f"      {line}")
                print()
                failed += 1
        except subprocess.TimeoutExpired:
            print("    → TIMEOUT (>300s)\n")
            failed += 1
        except Exception as e:
            print(f"    → ERROR: {e}\n")
            failed += 1
    return (succeeded, failed)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="GCA environment preflight")
    parser.add_argument("--quick", action="store_true", help="Skip network probes")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--fix", action="store_true", help="Auto-fix all blockers (including system packages)")
    parser.add_argument("--auto-fix", action="store_true",
                        help="Auto-fix safe blockers only (uv sync, npm install, playwright). "
                             "Used by the pipeline for unattended repair.")
    args = parser.parse_args()

    report = run_preflight(quick=args.quick)

    if (args.fix or args.auto_fix) and not report.all_pass:
        safe_only = args.auto_fix and not args.fix
        attempt_fixes(report, safe_only=safe_only)
        # Re-check after fixes
        report = run_preflight(quick=args.quick)

    print_report(report, json_mode=args.json)

    # Persist report. This is a REPO-level env check (not a per-game playtest),
    # so it lives under a repo cache dir — never a top-level `.playtest/`, which
    # is reserved for per-game verification workspaces.
    out = REPO_ROOT / ".cache" / "env-preflight-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), indent=2))

    sys.exit(1 if report.blockers else (2 if report.warnings else 0))


if __name__ == "__main__":
    main()
