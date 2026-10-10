"""Centralised configuration for the GCA MCP server.

Reads credentials and endpoints from environment variables (with `.env`
auto-loading) and exposes a typed :class:`Config` to the rest of the
package. One source of truth — no scattered ``os.environ.get`` calls
across adapters.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path
from typing import Any

DOUBAO_VLM_MODEL = "doubao-seed-2-0-pro-260215"
PLAY_WATCHDOG_GRACE_SECONDS = 15
SEEDANCE_FAST_MODEL = "doubao-seedance-2-0-fast-260128"

# ---- .env loader (no third-party dep) ------------------------------------


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_dotenv(extra_paths: Iterable[Path] = ()) -> None:
    """Load ``.env`` files. Order: explicit ``extra_paths`` → cwd → cwd parents.

    First-set wins (``setdefault`` semantics) so process env always has
    priority over file values.
    """
    seen: set[Path] = set()
    candidates: list[Path] = list(extra_paths)
    candidates.append(Path.cwd() / ".env")
    for parent in Path.cwd().parents:
        candidates.append(parent / ".env")
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        _load_env_file(path)


# ---- Config -------------------------------------------------------------


def _strip_yaml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in {"'", '"'}:
            quote = None if quote == ch else ch if quote is None else quote
            continue
        if ch == "#" and quote is None:
            return line[:idx]
    return line


def _parse_yaml_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the simple nested-map YAML used by gca.config.yaml.

    This intentionally supports only indentation-based mappings with scalar
    values. It keeps hooks and the MCP server free of a hard PyYAML dependency
    while still allowing comments and readable grouping.
    """

    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for raw_line in text.splitlines():
        line = _strip_yaml_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if ":" not in stripped:
            raise ValueError(f"Invalid YAML line: {raw_line!r}")
        key, _, raw_value = stripped.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"Invalid indentation near: {raw_line!r}")
        parent = stack[-1][1]
        if raw_value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_yaml_scalar(raw_value)
    return root


def _candidate_config_paths(path: Path | None) -> list[Path]:
    """Return only the explicitly selected config authority.

    Production passes ``<repo>/gca.config.yaml``. Silently falling back to a
    sibling JSON/YAML file made MCP defaults diverge from the gate process.
    """
    return [path] if path is not None else []


def _load_runtime_config(path: Path | None) -> dict[str, Any]:
    if path is not None and not path.is_file():
        raise FileNotFoundError(f"Required runtime config is missing: {path}")
    config_path = next((p for p in _candidate_config_paths(path) if p.is_file()), None)
    if config_path is None:
        return {}
    text = config_path.read_text(encoding="utf-8")
    try:
        if config_path.suffix in {".yaml", ".yml"}:
            data = _parse_simple_yaml(text)
        else:
            data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"Invalid runtime config at {config_path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"Runtime config must be an object: {config_path}")
    _validate_finite_config_numbers(data)
    return data


def _validate_finite_config_numbers(value: Any, path: str = "config") -> None:
    """Reject YAML/JSON NaN and infinities anywhere in runtime configuration."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be finite")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_finite_config_numbers(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_finite_config_numbers(item, f"{path}[{index}]")


def _get(data: Mapping[str, Any], path: str, default: Any) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _string_mapping(value: Any, path: str) -> dict[str, str]:
    """Return a normalized string mapping or reject a malformed config block."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{path} must map string keys to string values")
        normalized_key = key.strip().lower()
        normalized_value = item.strip()
        if not normalized_key or not normalized_value:
            raise ValueError(f"{path} keys and values must be non-empty strings")
        result[normalized_key] = normalized_value
    return result


def _strict_number(
    value: Any,
    path: str,
    *,
    minimum: float,
    maximum: float | None = None,
    integer: bool = False,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{path} must be a {'integer' if integer else 'number'}")
    if integer and not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    number = int(value) if integer else float(value)
    if not integer and not math.isfinite(number):
        raise ValueError(f"{path} must be finite")
    if number < minimum or (maximum is not None and number > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        raise ValueError(f"{path} must be >= {minimum}{upper}")
    return number


def _strict_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value


def _safe_bg_color(value: Any, path: str) -> str:
    if not isinstance(value, str) or value not in {"#FF00FF", "#00F000"}:
        raise ValueError(f"{path} must be #FF00FF or #00F000")
    return value


def _pinned_vlm_model(value: Any, path: str) -> str:
    model = str(value)
    if model != DOUBAO_VLM_MODEL:
        raise ValueError(f"{path} must be {DOUBAO_VLM_MODEL}")
    return model


def _single_vlm_attempt(value: Any, path: str) -> int:
    attempts = int(_strict_number(value, path, minimum=1, integer=True))
    if attempts != 1:
        raise ValueError(f"{path} must be exactly 1")
    return attempts


def _validate_configured_image_sizes(value: Any, path: str) -> None:
    mapping = _string_mapping(value, path)
    for key, size in mapping.items():
        try:
            width_text, separator, height_text = size.lower().partition("x")
            if not separator:
                raise ValueError
            width, height = int(width_text), int(height_text)
        except ValueError as exc:
            raise ValueError(f"{path}.{key} must be a WxH size") from exc
        area = width * height
        if (
            width <= 0
            or height <= 0
            or width % 16
            or height % 16
            or max(width, height) > 3840
            or max(width, height) / min(width, height) > 3
            or area < 655_360
            or area > 8_294_400
        ):
            raise ValueError(
                f"{path}.{key} is not a legal gpt-image-2 request size: {size!r}"
            )


def _ui_role_px_mapping(value: Any) -> dict[str, dict[str, int]]:
    """Parse the per-role, per-axis native-source guard."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("gates.ui_scale.role_px must be an object")
    result: dict[str, dict[str, int]] = {}
    for role, limits in value.items():
        if not isinstance(role, str) or not isinstance(limits, Mapping):
            raise ValueError(
                "gates.ui_scale.role_px must map roles to max_width/max_height"
            )
        if "max_width" not in limits or "max_height" not in limits:
            raise ValueError(
                f"gates.ui_scale.role_px.{role} requires max_width and max_height"
            )
        result[role.strip().lower()] = {
            "max_width": int(
                _strict_number(
                    limits["max_width"],
                    f"gates.ui_scale.role_px.{role}.max_width",
                    minimum=1,
                    integer=True,
                )
            ),
            "max_height": int(
                _strict_number(
                    limits["max_height"],
                    f"gates.ui_scale.role_px.{role}.max_height",
                    minimum=1,
                    integer=True,
                )
            ),
        }
    return result


def _require_runtime_authority(data: Mapping[str, Any]) -> None:
    ui_scale = _get(data, "gates.ui_scale", None)
    if not isinstance(ui_scale, Mapping):
        raise ValueError("gates.ui_scale is required and must be an object")
    for key in (
        "min_px",
        "max_texture_px",
        "aspect_tolerance",
        "require_display_size",
        "role_px",
        "max_canvas_fraction",
        "layout_check",
        "overlap_min_px",
    ):
        if key not in ui_scale:
            raise ValueError(f"gates.ui_scale.{key} is required")
    role_px = ui_scale.get("role_px")
    max_texture_px = _strict_number(
        ui_scale["max_texture_px"],
        "gates.ui_scale.max_texture_px",
        minimum=1,
        integer=True,
    )
    for role in ("hud", "panel"):
        if not isinstance(role_px, Mapping) or role not in role_px:
            raise ValueError(f"gates.ui_scale.role_px.{role} is required")
        limits = role_px[role]
        if isinstance(limits, Mapping):
            for axis in ("max_width", "max_height"):
                if (
                    axis in limits
                    and _strict_number(
                        limits[axis],
                        f"gates.ui_scale.role_px.{role}.{axis}",
                        minimum=1,
                        integer=True,
                    )
                    > max_texture_px
                ):
                    raise ValueError(
                        f"gates.ui_scale.role_px.{role}.{axis} cannot exceed "
                        "gates.ui_scale.max_texture_px"
                    )
    fractions = ui_scale.get("max_canvas_fraction")
    for role in ("hud", "panel", "banner"):
        if not isinstance(fractions, Mapping) or role not in fractions:
            raise ValueError(f"gates.ui_scale.max_canvas_fraction.{role} is required")
    for path in (
        "assets.image.size_by_asset_type",
        "assets.image.size_by_aspect",
    ):
        value = _get(data, path, None)
        if not isinstance(value, Mapping) or not value:
            raise ValueError(f"{path} is required and must be a non-empty object")
        _validate_configured_image_sizes(value, path)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean: true/false, yes/no, on/off, or 1/0")


@dataclass(frozen=True)
class PipelineConfig:
    min_iterations: int = 1
    max_iterations: int = 2
    # Dedicated fix -> re-verify budget for the multimodal-GDD VISUAL axis
    # (whole-screen + per-element). Independent of `max_iterations`, which stays
    # small for every other verify axis. Mirrors `gates.visual.max_rounds`.
    visual_max_iterations: int = 2
    max_consecutive_blocks: int = 5
    min_screenshots: int = 3
    min_unique_screenshots: int = 3
    staleness_tolerance_seconds: int = 60
    max_analysis_age_minutes: int = 30
    min_iteration_gap_seconds: int = 120
    max_counter_increment: int = 1
    audit_tolerance: int = 1

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> PipelineConfig:
        section = _get(data, "pipeline", {})
        config = cls(
            min_iterations=int(_get(section, "min_iterations", cls.min_iterations)),
            max_iterations=int(_get(section, "max_iterations", cls.max_iterations)),
            visual_max_iterations=int(
                _get(section, "visual_max_iterations", cls.visual_max_iterations)
            ),
            max_consecutive_blocks=int(
                _get(section, "max_consecutive_blocks", cls.max_consecutive_blocks)
            ),
            min_screenshots=int(_get(section, "min_screenshots", cls.min_screenshots)),
            min_unique_screenshots=int(
                _get(section, "min_unique_screenshots", cls.min_unique_screenshots)
            ),
            staleness_tolerance_seconds=int(
                _get(
                    section,
                    "staleness_tolerance_seconds",
                    cls.staleness_tolerance_seconds,
                )
            ),
            max_analysis_age_minutes=int(
                _get(
                    section,
                    "max_analysis_age_minutes",
                    cls.max_analysis_age_minutes,
                )
            ),
            min_iteration_gap_seconds=int(
                _get(
                    section,
                    "min_iteration_gap_seconds",
                    cls.min_iteration_gap_seconds,
                )
            ),
            max_counter_increment=int(
                _get(section, "max_counter_increment", cls.max_counter_increment)
            ),
            audit_tolerance=int(_get(section, "audit_tolerance", cls.audit_tolerance)),
        )
        if config.min_iterations < 0:
            raise ValueError("pipeline.min_iterations must be >= 0")
        if config.max_iterations < 1:
            raise ValueError("pipeline.max_iterations must be >= 1")
        if config.min_iterations > config.max_iterations:
            raise ValueError(
                "pipeline.min_iterations cannot exceed pipeline.max_iterations"
            )
        return config


@dataclass(frozen=True)
class RubricGateConfig:
    intention_min: float = 0.9
    visual_min: float = 0.7
    interaction_min: float = 0.8


@dataclass(frozen=True)
class PolishGateConfig:
    visual_rounds_min: int = 1
    gameplay_rounds_min: int = 1
    require_stress_tested: bool = False


@dataclass(frozen=True)
class VisualGateConfig:
    """Multimodal-GDD VISUAL verification loop (global + per-element image diff).

    Consumed by element-visual-diff.mjs (the mechanism) and gateVisualDiff (the
    gate). `max_rounds` is the dedicated visual fix -> re-verify budget, separate
    from PipelineConfig.max_iterations.
    """

    enabled: bool = True
    max_rounds: int = 2
    method: str = "paired_vlm"
    reference: str = "concept_crop"
    axis_global: bool = True
    axis_element: bool = True
    crop_padding_px: int = 6
    min_element_px: int = 16
    require_report: bool = True


@dataclass(frozen=True)
class UiScaleGateConfig:
    """Shared UI physical/fitness contract.

    Fields remain ``None`` when no runtime config authority was supplied. The
    production gate fails closed on missing keys; unit tests that construct a
    credential-only ``Config`` can still exercise unrelated adapters.
    """

    min_px: float | None = None
    max_texture_px: int | None = None
    aspect_tolerance: float | None = None
    require_display_size: bool | None = None
    role_px: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    max_canvas_fraction: Mapping[str, float] = field(default_factory=dict)
    layout_check: bool | None = None
    overlap_min_px: float | None = None


@dataclass(frozen=True)
class PerformanceGateConfig:
    min_fps_p50: float = 24.0
    min_fps_min: float = 10.0
    max_boot_seconds: float = 10.0
    max_long_task_count: int = 20
    max_long_task_total_ms: float = 2000.0
    max_memory_growth_mb: float = 50.0
    max_object_growth_count: int = 250
    max_object_slope_count_per_second: float = 10.0


@dataclass(frozen=True)
class GatesConfig:
    play_quality_min_accuracy: float = 0.8
    performance: PerformanceGateConfig = field(default_factory=PerformanceGateConfig)
    rubric: RubricGateConfig = field(default_factory=RubricGateConfig)
    polish: PolishGateConfig = field(default_factory=PolishGateConfig)
    visual: VisualGateConfig = field(default_factory=VisualGateConfig)
    ui_scale: UiScaleGateConfig = field(default_factory=UiScaleGateConfig)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> GatesConfig:
        section = _get(data, "gates", {})
        rubric = _get(section, "rubric", {})
        performance = _get(section, "performance", {})
        polish = _get(section, "polish", {})
        visual = _get(section, "visual", {})
        visual_axes = _get(visual, "axes", {})
        ui_scale = _get(section, "ui_scale", {})
        if ui_scale and not isinstance(ui_scale, Mapping):
            raise ValueError("gates.ui_scale must be an object")
        max_canvas_fraction = _get(ui_scale, "max_canvas_fraction", {})
        if max_canvas_fraction and not isinstance(max_canvas_fraction, Mapping):
            raise ValueError("gates.ui_scale.max_canvas_fraction must be an object")
        parsed_fractions = {
            str(key).strip().lower(): float(
                _strict_number(
                    value,
                    f"gates.ui_scale.max_canvas_fraction.{key}",
                    minimum=0.000001,
                    maximum=1.0,
                )
            )
            for key, value in (
                max_canvas_fraction.items()
                if isinstance(max_canvas_fraction, Mapping)
                else ()
            )
        }
        return cls(
            play_quality_min_accuracy=float(
                _get(
                    section,
                    "play_quality_min_accuracy",
                    cls.play_quality_min_accuracy,
                )
            ),
            polish=PolishGateConfig(
                visual_rounds_min=int(
                    _get(
                        polish, "visual_rounds_min", PolishGateConfig.visual_rounds_min
                    )
                ),
                gameplay_rounds_min=int(
                    _get(
                        polish,
                        "gameplay_rounds_min",
                        PolishGateConfig.gameplay_rounds_min,
                    )
                ),
                require_stress_tested=bool(
                    _get(
                        polish,
                        "require_stress_tested",
                        PolishGateConfig.require_stress_tested,
                    )
                ),
            ),
            visual=VisualGateConfig(
                enabled=bool(_get(visual, "enabled", VisualGateConfig.enabled)),
                max_rounds=int(_get(visual, "max_rounds", VisualGateConfig.max_rounds)),
                method=str(_get(visual, "method", VisualGateConfig.method)),
                reference=str(_get(visual, "reference", VisualGateConfig.reference)),
                axis_global=bool(
                    _get(visual_axes, "global", VisualGateConfig.axis_global)
                ),
                axis_element=bool(
                    _get(visual_axes, "element", VisualGateConfig.axis_element)
                ),
                crop_padding_px=int(
                    _get(visual, "crop_padding_px", VisualGateConfig.crop_padding_px)
                ),
                min_element_px=int(
                    _get(visual, "min_element_px", VisualGateConfig.min_element_px)
                ),
                require_report=bool(
                    _get(visual, "require_report", VisualGateConfig.require_report)
                ),
            ),
            ui_scale=UiScaleGateConfig(
                min_px=(
                    float(
                        _strict_number(
                            ui_scale["min_px"],
                            "gates.ui_scale.min_px",
                            minimum=0.000001,
                        )
                    )
                    if isinstance(ui_scale, Mapping) and "min_px" in ui_scale
                    else None
                ),
                max_texture_px=(
                    int(
                        _strict_number(
                            ui_scale["max_texture_px"],
                            "gates.ui_scale.max_texture_px",
                            minimum=1,
                            integer=True,
                        )
                    )
                    if isinstance(ui_scale, Mapping) and "max_texture_px" in ui_scale
                    else None
                ),
                aspect_tolerance=(
                    float(
                        _strict_number(
                            ui_scale["aspect_tolerance"],
                            "gates.ui_scale.aspect_tolerance",
                            minimum=0,
                            maximum=1,
                        )
                    )
                    if isinstance(ui_scale, Mapping) and "aspect_tolerance" in ui_scale
                    else None
                ),
                require_display_size=(
                    _strict_bool(
                        ui_scale["require_display_size"],
                        "gates.ui_scale.require_display_size",
                    )
                    if isinstance(ui_scale, Mapping)
                    and "require_display_size" in ui_scale
                    else None
                ),
                role_px=_ui_role_px_mapping(
                    _get(ui_scale, "role_px", None)
                    if isinstance(ui_scale, Mapping)
                    else None
                ),
                max_canvas_fraction=parsed_fractions,
                layout_check=(
                    _strict_bool(
                        ui_scale["layout_check"],
                        "gates.ui_scale.layout_check",
                    )
                    if isinstance(ui_scale, Mapping) and "layout_check" in ui_scale
                    else None
                ),
                overlap_min_px=(
                    float(
                        _strict_number(
                            ui_scale["overlap_min_px"],
                            "gates.ui_scale.overlap_min_px",
                            minimum=0,
                            maximum=4096,
                        )
                    )
                    if isinstance(ui_scale, Mapping) and "overlap_min_px" in ui_scale
                    else None
                ),
            ),
            performance=PerformanceGateConfig(
                min_fps_p50=float(
                    _get(performance, "min_fps_p50", PerformanceGateConfig.min_fps_p50)
                ),
                min_fps_min=float(
                    _get(performance, "min_fps_min", PerformanceGateConfig.min_fps_min)
                ),
                max_boot_seconds=float(
                    _get(
                        performance,
                        "max_boot_seconds",
                        PerformanceGateConfig.max_boot_seconds,
                    )
                ),
                max_long_task_count=int(
                    _get(
                        performance,
                        "max_long_task_count",
                        PerformanceGateConfig.max_long_task_count,
                    )
                ),
                max_long_task_total_ms=float(
                    _get(
                        performance,
                        "max_long_task_total_ms",
                        PerformanceGateConfig.max_long_task_total_ms,
                    )
                ),
                max_memory_growth_mb=float(
                    _get(
                        performance,
                        "max_memory_growth_mb",
                        PerformanceGateConfig.max_memory_growth_mb,
                    )
                ),
                max_object_growth_count=int(
                    _get(
                        performance,
                        "max_object_growth_count",
                        PerformanceGateConfig.max_object_growth_count,
                    )
                ),
                max_object_slope_count_per_second=float(
                    _get(
                        performance,
                        "max_object_slope_count_per_second",
                        PerformanceGateConfig.max_object_slope_count_per_second,
                    )
                ),
            ),
            rubric=RubricGateConfig(
                intention_min=float(
                    _get(rubric, "intention_min", RubricGateConfig.intention_min)
                ),
                visual_min=float(
                    _get(rubric, "visual_min", RubricGateConfig.visual_min)
                ),
                interaction_min=float(
                    _get(rubric, "interaction_min", RubricGateConfig.interaction_min)
                ),
            ),
        )


@dataclass(frozen=True)
class AssetImageDefaults:
    request_timeout_seconds: int = 310
    default_size: str = "auto"
    edit_default_size: str = "1024x1024"
    size_by_asset_type: Mapping[str, str] = field(default_factory=dict)
    size_by_aspect: Mapping[str, str] = field(default_factory=dict)
    default_quality: str = "low"
    max_retries: int = 3
    retry_delay_seconds: float = 10.0
    enforce_safe_bg_avoidance: bool = True
    # Canvas-fill color used when a matted asset_type (prop/tile/ui/icon/…) is
    # generated/edited WITHOUT an explicit ``safe_bg_color`` — so its background
    # is removable downstream instead of an arbitrary model-picked color.
    safe_bg_color: str = "#FF00FF"


@dataclass(frozen=True)
class AssetSeedanceDefaults:
    model: str = "fast"
    duration: int = 4
    ratio: str = "16:9"
    sprite_ratio: str = "1:1"
    resolution: str = "480p"
    server_timeout_seconds: int = 600
    download_timeout_seconds: int = 120
    max_quality_candidates: int = 1
    transient_max_attempts: int = 3
    transient_retry_delay_seconds: float = 5.0


@dataclass(frozen=True)
class AssetRmbgDefaults:
    aug_preset: str = "off"
    rmbg_fg_cutoff: int = 230
    max_size: int = 960
    workers: int = 8
    gif_max_size: int = 960
    gif_alpha_threshold: int = 200
    keep_frames: bool = True
    timeout_seconds: int = 60
    align_feet_to_bottom: bool = False
    foot_margin_px: int = 0
    drop_bg_leak: bool = True
    bg_leak_area_threshold: float = 0.005
    bg_leak_color_dist: int = 120
    bg_leak_alpha_threshold: int = 128
    bg_leak_keep_min_frames: int = 2
    bg_leak_alpha_weighted: bool = True


@dataclass(frozen=True)
class AssetSpriteDefaults:
    min_frame_dimension_px: int = 64
    safe_bg_color: str = "#FF00FF"
    image_size: str = "1024x1024"
    image_quality: str = "low"
    image_max_attempts: int = 3
    duration: int = 4
    ratio: str = "1:1"
    resolution: str = "480p"
    seedance_model: str = "fast"
    aug_preset: str = "off"
    rmbg_fg_cutoff: int = 230
    gif_alpha_threshold: int = 200
    align_feet_to_bottom: bool = True
    foot_margin_px: int = 2
    matte_workers: int = 8
    fps: int = 12
    max_frames: int = 24
    max_sheet_width: int = 4096
    # Runtime-only FX contract (PR #123 review #8): FX (trails, muzzle flashes,
    # spells, impacts, explosions) are runtime ParticleEmitter recipes, not baked
    # into generated sheets — so body-animation generation defaults to NO
    # generated effects. (Was True, which asked Seedance to bake FX that the
    # runtime then duplicated.)
    effects: bool = False


@dataclass(frozen=True)
class AssetVlmDefaults:
    model: str = DOUBAO_VLM_MODEL
    image_max_tokens: int = 1200
    detect_max_tokens: int = 2500
    image_timeout_seconds: float = 90.0
    slot_timeout_seconds: float = 60.0
    slot_attempts_per_model: int = 1
    video_max_tokens: int = 1500
    video_timeout_seconds: float = 180.0
    temperature: float = 0.0


@dataclass(frozen=True)
class AssetMattingDefaults:
    """Which matting backend generated static and dynamic assets use.

    ``color_key`` is the default: pure-computation safe-color matting that
    needs no model and no API, so GCA downloads no model weights out of the box.
    ``auto`` prefers RMBG-2.0 and falls back to deterministic color-key matting
    for network, missing-route HTTP 404, or HTTP 5xx outages.
    ``rmbg2`` requires the RMBG-2.0 service (remote or local deployment).

    ``auto`` and ``rmbg2`` load RMBG-2.0, which is licensed under CC BY-NC 4.0
    and prohibits commercial use. Selecting either value is an explicit opt-in;
    obtaining the model and complying with its license is the operator's
    responsibility.
    """

    backend: str = "color_key"
    color_key_threshold: int = 100
    color_key_edge_threshold: int = 150

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> AssetMattingDefaults:
        if not isinstance(data, Mapping):
            raise ValueError("assets.matting must be an object")
        backend = _get(data, "backend", cls.backend)
        if not isinstance(backend, str) or backend not in {
            "auto",
            "rmbg2",
            "color_key",
        }:
            raise ValueError(
                "assets.matting.backend must be one of: auto, rmbg2, color_key"
            )
        threshold = int(
            _strict_number(
                _get(data, "color_key_threshold", cls.color_key_threshold),
                "assets.matting.color_key_threshold",
                minimum=0,
                maximum=255,
                integer=True,
            )
        )
        edge_threshold = int(
            _strict_number(
                _get(
                    data,
                    "color_key_edge_threshold",
                    cls.color_key_edge_threshold,
                ),
                "assets.matting.color_key_edge_threshold",
                minimum=1,
                maximum=255,
                integer=True,
            )
        )
        if edge_threshold < threshold:
            raise ValueError(
                "assets.matting.color_key_edge_threshold must be greater than "
                "or equal to assets.matting.color_key_threshold"
            )
        return cls(
            backend=backend,
            color_key_threshold=threshold,
            color_key_edge_threshold=edge_threshold,
        )


@dataclass(frozen=True)
class AssetsRuntimeConfig:
    image: AssetImageDefaults = field(default_factory=AssetImageDefaults)
    seedance: AssetSeedanceDefaults = field(default_factory=AssetSeedanceDefaults)
    rmbg: AssetRmbgDefaults = field(default_factory=AssetRmbgDefaults)
    sprite: AssetSpriteDefaults = field(default_factory=AssetSpriteDefaults)
    vlm: AssetVlmDefaults = field(default_factory=AssetVlmDefaults)
    matting: AssetMattingDefaults = field(default_factory=AssetMattingDefaults)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> AssetsRuntimeConfig:
        root = _get(data, "assets", {})
        image = _get(root, "image", {})
        seedance = _get(root, "seedance", {})
        rmbg = _get(root, "rmbg", {})
        sprite = _get(root, "sprite", {})
        vlm = _get(root, "vlm", {})
        matting = _get(root, "matting", {})
        legacy_video_attempts = _get(sprite, "video_n_candidates", None)
        return cls(
            image=AssetImageDefaults(
                request_timeout_seconds=int(
                    _get(image, "request_timeout_seconds", 310)
                ),
                default_size=str(_get(image, "default_size", "auto")),
                edit_default_size=str(_get(image, "edit_default_size", "1024x1024")),
                size_by_asset_type=_string_mapping(
                    _get(image, "size_by_asset_type", None),
                    "assets.image.size_by_asset_type",
                ),
                size_by_aspect=_string_mapping(
                    _get(image, "size_by_aspect", None),
                    "assets.image.size_by_aspect",
                ),
                default_quality=str(_get(image, "default_quality", "low")),
                max_retries=int(_get(image, "max_retries", 3)),
                retry_delay_seconds=float(_get(image, "retry_delay_seconds", 10.0)),
                enforce_safe_bg_avoidance=bool(
                    _get(image, "enforce_safe_bg_avoidance", True)
                ),
                safe_bg_color=_safe_bg_color(
                    _get(image, "safe_bg_color", "#FF00FF"),
                    "assets.image.safe_bg_color",
                ),
            ),
            seedance=AssetSeedanceDefaults(
                model=str(_get(seedance, "model", "fast")),
                duration=int(_get(seedance, "duration", 4)),
                ratio=str(_get(seedance, "ratio", "16:9")),
                sprite_ratio=str(_get(seedance, "sprite_ratio", "1:1")),
                resolution=str(_get(seedance, "resolution", "480p")),
                server_timeout_seconds=int(
                    _get(seedance, "server_timeout_seconds", 600)
                ),
                download_timeout_seconds=int(
                    _get(seedance, "download_timeout_seconds", 120)
                ),
                max_quality_candidates=int(
                    _strict_number(
                        _get(
                            seedance,
                            "max_quality_candidates",
                            (
                                legacy_video_attempts
                                if legacy_video_attempts is not None
                                else AssetSeedanceDefaults.max_quality_candidates
                            ),
                        ),
                        "assets.seedance.max_quality_candidates",
                        minimum=1,
                        integer=True,
                    )
                ),
                transient_max_attempts=int(
                    _strict_number(
                        _get(
                            seedance,
                            "transient_max_attempts",
                            AssetSeedanceDefaults.transient_max_attempts,
                        ),
                        "assets.seedance.transient_max_attempts",
                        minimum=1,
                        integer=True,
                    )
                ),
                transient_retry_delay_seconds=float(
                    _strict_number(
                        _get(
                            seedance,
                            "transient_retry_delay_seconds",
                            AssetSeedanceDefaults.transient_retry_delay_seconds,
                        ),
                        "assets.seedance.transient_retry_delay_seconds",
                        minimum=0,
                    )
                ),
            ),
            rmbg=AssetRmbgDefaults(
                aug_preset=str(_get(rmbg, "aug_preset", "off")),
                rmbg_fg_cutoff=int(_get(rmbg, "rmbg_fg_cutoff", 230)),
                max_size=int(_get(rmbg, "max_size", 960)),
                workers=int(_get(rmbg, "workers", 8)),
                gif_max_size=int(_get(rmbg, "gif_max_size", 960)),
                gif_alpha_threshold=int(_get(rmbg, "gif_alpha_threshold", 200)),
                keep_frames=bool(_get(rmbg, "keep_frames", True)),
                timeout_seconds=int(_get(rmbg, "timeout_seconds", 60)),
                align_feet_to_bottom=bool(_get(rmbg, "align_feet_to_bottom", False)),
                foot_margin_px=int(_get(rmbg, "foot_margin_px", 0)),
                drop_bg_leak=bool(_get(rmbg, "drop_bg_leak", True)),
                bg_leak_area_threshold=float(
                    _get(rmbg, "bg_leak_area_threshold", 0.005)
                ),
                bg_leak_color_dist=int(_get(rmbg, "bg_leak_color_dist", 120)),
                bg_leak_alpha_threshold=int(_get(rmbg, "bg_leak_alpha_threshold", 128)),
                bg_leak_keep_min_frames=int(_get(rmbg, "bg_leak_keep_min_frames", 2)),
                bg_leak_alpha_weighted=bool(_get(rmbg, "bg_leak_alpha_weighted", True)),
            ),
            sprite=AssetSpriteDefaults(
                min_frame_dimension_px=int(_get(sprite, "min_frame_dimension_px", 64)),
                safe_bg_color=_safe_bg_color(
                    _get(sprite, "safe_bg_color", "#FF00FF"),
                    "assets.sprite.safe_bg_color",
                ),
                image_size=str(_get(sprite, "image_size", "1024x1024")),
                image_quality=str(_get(sprite, "image_quality", "low")),
                image_max_attempts=int(_get(sprite, "image_max_attempts", 3)),
                duration=int(_get(sprite, "duration", 4)),
                ratio=str(_get(sprite, "ratio", "1:1")),
                resolution=str(_get(sprite, "resolution", "480p")),
                seedance_model=str(_get(sprite, "seedance_model", "fast")),
                aug_preset=str(_get(sprite, "aug_preset", "off")),
                rmbg_fg_cutoff=int(_get(sprite, "rmbg_fg_cutoff", 230)),
                gif_alpha_threshold=int(_get(sprite, "gif_alpha_threshold", 200)),
                align_feet_to_bottom=bool(_get(sprite, "align_feet_to_bottom", True)),
                foot_margin_px=int(_get(sprite, "foot_margin_px", 2)),
                matte_workers=int(_get(sprite, "matte_workers", 8)),
                fps=int(_get(sprite, "fps", 12)),
                max_frames=int(_get(sprite, "max_frames", 24)),
                max_sheet_width=int(_get(sprite, "max_sheet_width", 4096)),
                effects=bool(_get(sprite, "effects", False)),
            ),
            vlm=AssetVlmDefaults(
                model=_pinned_vlm_model(
                    _get(vlm, "model", DOUBAO_VLM_MODEL),
                    "assets.vlm.model",
                ),
                image_max_tokens=int(
                    _strict_number(
                        _get(vlm, "image_max_tokens", 1200),
                        "assets.vlm.image_max_tokens",
                        minimum=1,
                        integer=True,
                    )
                ),
                detect_max_tokens=int(
                    _strict_number(
                        _get(vlm, "detect_max_tokens", 2500),
                        "assets.vlm.detect_max_tokens",
                        minimum=1,
                        integer=True,
                    )
                ),
                image_timeout_seconds=float(_get(vlm, "image_timeout_seconds", 90.0)),
                slot_timeout_seconds=float(
                    _strict_number(
                        _get(vlm, "slot_timeout_seconds", 60.0),
                        "assets.vlm.slot_timeout_seconds",
                        minimum=1,
                    )
                ),
                slot_attempts_per_model=_single_vlm_attempt(
                    _get(vlm, "slot_attempts_per_model", 1),
                    "assets.vlm.slot_attempts_per_model",
                ),
                video_max_tokens=int(
                    _strict_number(
                        _get(vlm, "video_max_tokens", 1500),
                        "assets.vlm.video_max_tokens",
                        minimum=1,
                        integer=True,
                    )
                ),
                video_timeout_seconds=float(_get(vlm, "video_timeout_seconds", 180.0)),
                temperature=float(_get(vlm, "temperature", 0.0)),
            ),
            matting=AssetMattingDefaults.from_mapping(matting),
        )


@dataclass(frozen=True)
class VerifyRuntimeConfig:
    run_tests_timeout_seconds: float = 1200.0
    play_game_timeout_seconds: float = 1200.0
    play_max_wall_seconds: int = 240
    max_parallel_producers: int = 5
    max_parallel_provider_producers: int = 2
    record_duration_seconds: int = 30
    record_timeout_seconds: float = 600.0
    video_analysis_timeout_seconds: float = 1800.0
    # Skip the video verification pipeline (record_with_state + analyze_video +
    # the playthrough/performance gates). The no-video axes still run and gate.
    skip_video: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> VerifyRuntimeConfig:
        section = _get(data, "verify", {})
        config = cls(
            run_tests_timeout_seconds=float(
                _get(
                    section,
                    "run_tests_timeout_seconds",
                    cls.run_tests_timeout_seconds,
                )
            ),
            play_game_timeout_seconds=float(
                _strict_number(
                    _get(
                        section,
                        "play_game_timeout_seconds",
                        cls.play_game_timeout_seconds,
                    ),
                    "verify.play_game_timeout_seconds",
                    minimum=1,
                )
            ),
            play_max_wall_seconds=int(
                _strict_number(
                    _get(
                        section,
                        "play_max_wall_seconds",
                        cls.play_max_wall_seconds,
                    ),
                    "verify.play_max_wall_seconds",
                    minimum=30,
                    integer=True,
                )
            ),
            max_parallel_producers=int(
                _strict_number(
                    _get(
                        section,
                        "max_parallel_producers",
                        cls.max_parallel_producers,
                    ),
                    "verify.max_parallel_producers",
                    minimum=1,
                    integer=True,
                )
            ),
            max_parallel_provider_producers=int(
                _strict_number(
                    _get(
                        section,
                        "max_parallel_provider_producers",
                        cls.max_parallel_provider_producers,
                    ),
                    "verify.max_parallel_provider_producers",
                    minimum=1,
                    integer=True,
                )
            ),
            record_duration_seconds=int(
                _get(section, "record_duration_seconds", cls.record_duration_seconds)
            ),
            record_timeout_seconds=float(
                _get(section, "record_timeout_seconds", cls.record_timeout_seconds)
            ),
            video_analysis_timeout_seconds=float(
                _get(
                    section,
                    "video_analysis_timeout_seconds",
                    cls.video_analysis_timeout_seconds,
                )
            ),
            skip_video=bool(_get(section, "skip_video", cls.skip_video)),
        )
        if (
            config.play_game_timeout_seconds
            < config.play_max_wall_seconds + PLAY_WATCHDOG_GRACE_SECONDS
        ):
            raise ValueError(
                "verify.play_game_timeout_seconds must be at least "
                "verify.play_max_wall_seconds + "
                f"{PLAY_WATCHDOG_GRACE_SECONDS} seconds"
            )
        return config


@dataclass(frozen=True)
class DesignConfig:
    max_sprite_entities: int = 4
    max_actions_per_entity: int = 3
    max_sprite_action_rows: int = 15
    max_background_layers: int = 4
    max_prop_packs: int = 3
    max_ui_elements: int = 4
    # Global cap on AI concept mockups generated in design (multimodal GDD,
    # risk #9): aim for ~max_concept_images, never exceed hard_cap. WHICH
    # screens to draw is chosen per game; the COUNT is bounded here.
    max_concept_images: int = 6
    concept_images_hard_cap: int = 8

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> DesignConfig:
        section = _get(data, "design", {})
        return cls(
            max_sprite_entities=int(
                _get(section, "max_sprite_entities", cls.max_sprite_entities)
            ),
            max_actions_per_entity=int(
                _get(section, "max_actions_per_entity", cls.max_actions_per_entity)
            ),
            max_sprite_action_rows=int(
                _get(section, "max_sprite_action_rows", cls.max_sprite_action_rows)
            ),
            max_background_layers=int(
                _get(section, "max_background_layers", cls.max_background_layers)
            ),
            max_prop_packs=int(_get(section, "max_prop_packs", cls.max_prop_packs)),
            max_ui_elements=int(_get(section, "max_ui_elements", cls.max_ui_elements)),
            max_concept_images=int(
                _get(section, "max_concept_images", cls.max_concept_images)
            ),
            concept_images_hard_cap=int(
                _get(section, "concept_images_hard_cap", cls.concept_images_hard_cap)
            ),
        )


@dataclass(frozen=True)
class ImageGenConfig:
    # Provider defaults; overridable via GAME_ASSET_BASE_URL / GAME_ASSET_MODEL.
    base_url: str = "https://api.openai.com/v1"
    model_id: str = "gpt-image-2"


@dataclass(frozen=True)
class SeedanceConfig:
    # ARK native task endpoint (submit → poll → download).
    url: str = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
    verify_ssl: bool = True
    provider: str = "doubao"
    poll_interval_seconds: float = 10.0
    # preset → model id. Public-safe defaults; overridable via env
    # (SEEDANCE_MODEL_STANDARD / SEEDANCE_MODEL_FAST) or model_id.
    models: dict[str, str] = field(
        default_factory=lambda: {
            "standard": "doubao-seedance-2-0-260128",
            "fast": SEEDANCE_FAST_MODEL,
        }
    )


@dataclass(frozen=True)
class Rmbg2Config:
    """RMBG-2.0 background removal.

    A configured ``RMBG_BASE_URL`` uses the remote service. An empty endpoint
    selects the local in-process implementation.
    """

    endpoint: str = ""


@dataclass(frozen=True)
class DoubaoConfig:
    """Doubao multimodal understanding through ARK's compatible chat API."""

    endpoint: str = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    model: str = DOUBAO_VLM_MODEL


# --------------------------------------------------------------------------
# VLM model policy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VlmPolicyConfig:
    """Pinned VLM single-attempt policy."""

    attempts_per_model: int = 1
    backoff_seconds: float = 2.0
    rate_limit_backoff_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> VlmPolicyConfig:
        attempts_per_model = int(os.environ.get("VLM_ATTEMPTS_PER_MODEL", "1"))
        if attempts_per_model != 1:
            raise ValueError("VLM_ATTEMPTS_PER_MODEL must be exactly 1")
        return cls(
            attempts_per_model=attempts_per_model,
            backoff_seconds=float(os.environ.get("VLM_BACKOFF_SECONDS", "2.0")),
            rate_limit_backoff_seconds=float(
                os.environ.get("VLM_RATE_LIMIT_BACKOFF_SECONDS", "10.0")
            ),
        )


@dataclass
class Config:
    """Top-level config bag passed through tool handlers."""

    image: ImageGenConfig
    seedance: SeedanceConfig
    rmbg2: Rmbg2Config
    doubao: DoubaoConfig
    vlm_policy: VlmPolicyConfig = field(default_factory=VlmPolicyConfig.from_env)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    gates: GatesConfig = field(default_factory=GatesConfig)
    design: DesignConfig = field(default_factory=DesignConfig)
    assets: AssetsRuntimeConfig = field(default_factory=AssetsRuntimeConfig)
    verify: VerifyRuntimeConfig = field(default_factory=VerifyRuntimeConfig)

    @classmethod
    def from_env(
        cls,
        *,
        config_path: Path | None = None,
    ) -> Config:
        runtime_config = _load_runtime_config(config_path)
        if config_path is not None:
            _require_runtime_authority(runtime_config)
        return cls(
            image=ImageGenConfig(
                base_url=os.environ.get(
                    "GAME_ASSET_BASE_URL",
                    ImageGenConfig.__dataclass_fields__["base_url"].default,
                ),
                model_id=os.environ.get(
                    "GAME_ASSET_MODEL",
                    ImageGenConfig.__dataclass_fields__["model_id"].default,
                ),
            ),
            seedance=SeedanceConfig(
                url=os.environ.get(
                    "SEEDANCE_URL",
                    SeedanceConfig.__dataclass_fields__["url"].default,
                ),
                verify_ssl=_env_bool(
                    "SEEDANCE_VERIFY_SSL",
                    SeedanceConfig.__dataclass_fields__["verify_ssl"].default,
                ),
                provider=os.environ.get(
                    "SEEDANCE_PROVIDER",
                    SeedanceConfig.__dataclass_fields__["provider"].default,
                ),
                poll_interval_seconds=float(
                    os.environ.get(
                        "SEEDANCE_POLL_INTERVAL",
                        SeedanceConfig.__dataclass_fields__[
                            "poll_interval_seconds"
                        ].default,
                    )
                ),
                models={
                    "standard": os.environ.get(
                        "SEEDANCE_MODEL_STANDARD",
                        SeedanceConfig.__dataclass_fields__["models"].default_factory()[
                            "standard"
                        ],
                    ),
                    "fast": os.environ.get(
                        "SEEDANCE_MODEL_FAST",
                        SeedanceConfig.__dataclass_fields__["models"].default_factory()[
                            "fast"
                        ],
                    ),
                },
            ),
            rmbg2=Rmbg2Config(
                endpoint=os.environ.get(
                    "RMBG_BASE_URL",
                    Rmbg2Config.__dataclass_fields__["endpoint"].default,
                ),
            ),
            doubao=DoubaoConfig(
                endpoint=os.environ.get(
                    "DOUBAO_API_ENDPOINT",
                    DoubaoConfig.__dataclass_fields__["endpoint"].default,
                ),
                model=DOUBAO_VLM_MODEL,
            ),
            vlm_policy=VlmPolicyConfig.from_env(),
            pipeline=PipelineConfig.from_mapping(runtime_config),
            gates=GatesConfig.from_mapping(runtime_config),
            design=DesignConfig.from_mapping(runtime_config),
            assets=AssetsRuntimeConfig.from_mapping(runtime_config),
            verify=VerifyRuntimeConfig.from_mapping(runtime_config),
        )


def load_runtime_config(path: Path | None) -> dict[str, Any]:
    """Load and return the parsed runtime config dict (public wrapper)."""
    return _load_runtime_config(path)


__all__ = [
    "DOUBAO_VLM_MODEL",
    "PLAY_WATCHDOG_GRACE_SECONDS",
    "SEEDANCE_FAST_MODEL",
    "AssetsRuntimeConfig",
    "Config",
    "DesignConfig",
    "DoubaoConfig",
    "GatesConfig",
    "ImageGenConfig",
    "PerformanceGateConfig",
    "PipelineConfig",
    "Rmbg2Config",
    "SeedanceConfig",
    "VerifyRuntimeConfig",
    "VlmPolicyConfig",
    "load_dotenv",
    "load_runtime_config",
]
