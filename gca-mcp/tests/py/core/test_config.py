from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from gca_mcp.config import (
    DOUBAO_VLM_MODEL,
    SEEDANCE_FAST_MODEL,
    AssetSeedanceDefaults,
    Config,
    PipelineConfig,
    VisualGateConfig,
    load_runtime_config,
)


def _write_runtime_config(tmp_path, mutate) -> Path:
    source = Path(__file__).parents[4] / "gca.config.yaml"
    payload = copy.deepcopy(load_runtime_config(source))
    mutate(payload)
    config_path = tmp_path / "gca.config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_max_tokens", "-1"),
        ("detect_max_tokens", "0"),
        ("video_max_tokens", "1.5"),
    ],
)
def test_runtime_config_requires_positive_integer_token_budgets(
    tmp_path,
    field: str,
    value: str,
) -> None:
    parsed_value = float(value) if "." in value else int(value)
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["assets"]["vlm"].__setitem__(field, parsed_value),
    )

    with pytest.raises(ValueError, match=field):
        Config.from_env(config_path=config_path)


def test_runtime_config_loads_fast_slot_validation_policy() -> None:
    config = Config.from_env()

    assert config.assets.vlm.slot_timeout_seconds == 60
    assert config.assets.vlm.slot_attempts_per_model == 1


def test_vlm_policy_defaults_to_one_doubao_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VLM_ATTEMPTS_PER_MODEL", raising=False)

    config = Config.from_env()

    assert config.doubao.model == DOUBAO_VLM_MODEL
    assert config.assets.vlm.model == DOUBAO_VLM_MODEL
    assert config.vlm_policy.attempts_per_model == 1


def test_vlm_policy_rejects_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLM_ATTEMPTS_PER_MODEL", "2")

    with pytest.raises(ValueError, match="exactly 1"):
        Config.from_env()


def test_runtime_config_rejects_vlm_model_override(tmp_path: Path) -> None:
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["assets"]["vlm"].__setitem__(
            "model",
            "another-model",
        ),
    )

    with pytest.raises(ValueError, match="assets.vlm.model"):
        Config.from_env(config_path=config_path)


def test_verify_iteration_defaults_are_two_rounds() -> None:
    config = Config.from_env()

    assert PipelineConfig.max_iterations == 2
    assert PipelineConfig.visual_max_iterations == 2
    assert VisualGateConfig.max_rounds == 2
    assert config.pipeline.max_iterations == 2
    assert config.pipeline.visual_max_iterations == 2
    assert config.gates.visual.max_rounds == 2


@pytest.mark.parametrize(
    ("pipeline", "message"),
    [
        ({"min_iterations": -1, "max_iterations": 2}, "min_iterations"),
        ({"min_iterations": 0, "max_iterations": 0}, "max_iterations"),
        (
            {"min_iterations": 3, "max_iterations": 2},
            "cannot exceed",
        ),
    ],
)
def test_pipeline_rejects_invalid_iteration_budget(
    pipeline: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        PipelineConfig.from_mapping({"pipeline": pipeline})


def test_seedance_loads_ark_endpoint(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "SEEDANCE_URL",
        "https://ark.example/api/v3/contents/generations/tasks",
    )
    config = Config.from_env()

    assert config.seedance.url.endswith("/api/v3/contents/generations/tasks")


def test_seedance_fast_model_is_global_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEEDANCE_MODEL_FAST", raising=False)

    config = Config.from_env()

    assert config.seedance.models["fast"] == SEEDANCE_FAST_MODEL
    assert SEEDANCE_FAST_MODEL == "doubao-seedance-2-0-fast-260128"


def test_video_generation_defaults_to_one_quality_candidate() -> None:
    config = Config.from_env()

    assert AssetSeedanceDefaults.max_quality_candidates == 1
    assert config.assets.seedance.max_quality_candidates == 1
    assert config.assets.seedance.transient_max_attempts == 3


def test_video_quality_candidate_limit_is_global(tmp_path: Path) -> None:
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["assets"]["seedance"].__setitem__(
            "max_quality_candidates",
            2,
        ),
    )

    assert (
        Config.from_env(config_path=config_path)
        .assets.seedance.max_quality_candidates
        == 2
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_quality_candidates", 0),
        ("transient_max_attempts", 0),
        ("transient_retry_delay_seconds", -1),
    ],
)
def test_runtime_config_rejects_invalid_video_attempt_policy(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["assets"]["seedance"].__setitem__(field, value),
    )

    with pytest.raises(ValueError, match=field):
        Config.from_env(config_path=config_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("slot_timeout_seconds", 0),
        ("slot_attempts_per_model", 0),
        ("slot_attempts_per_model", 1.5),
        ("slot_attempts_per_model", 2),
    ],
)
def test_runtime_config_rejects_invalid_slot_validation_policy(
    tmp_path,
    field,
    value,
) -> None:
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["assets"]["vlm"].__setitem__(field, value),
    )

    with pytest.raises(ValueError, match=field):
        Config.from_env(config_path=config_path)


def test_runtime_config_rejects_nan_float_threshold(tmp_path) -> None:
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["gates"]["rubric"].__setitem__(
            "visual_min",
            float("nan"),
        ),
    )

    with pytest.raises(ValueError, match=r"visual_min must be finite"):
        Config.from_env(config_path=config_path)


@pytest.mark.parametrize("backend", ["color-key", "typo", 1, None])
def test_runtime_config_rejects_unknown_matting_backend(
    tmp_path,
    backend,
) -> None:
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["assets"]["matting"].__setitem__("backend", backend),
    )

    with pytest.raises(ValueError, match=r"assets\.matting\.backend"):
        Config.from_env(config_path=config_path)


@pytest.mark.parametrize("backend", ["auto", "rmbg2", "color_key"])
def test_runtime_config_accepts_supported_matting_backends(
    tmp_path,
    backend: str,
) -> None:
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["assets"]["matting"].__setitem__("backend", backend),
    )

    assert Config.from_env(config_path=config_path).assets.matting.backend == backend


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("color_key_threshold", -1),
        ("color_key_threshold", 256),
        ("color_key_threshold", 1.5),
        ("color_key_edge_threshold", -1),
        ("color_key_edge_threshold", 0),
        ("color_key_edge_threshold", 256),
        ("color_key_edge_threshold", True),
    ],
)
def test_runtime_config_rejects_invalid_color_key_thresholds(
    tmp_path,
    field: str,
    value,
) -> None:
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["assets"]["matting"].__setitem__(field, value),
    )

    with pytest.raises(ValueError, match=field):
        Config.from_env(config_path=config_path)


def test_runtime_config_requires_edge_threshold_at_least_global_threshold(
    tmp_path,
) -> None:
    def mutate(config) -> None:
        config["assets"]["matting"]["color_key_threshold"] = 151
        config["assets"]["matting"]["color_key_edge_threshold"] = 150

    config_path = _write_runtime_config(tmp_path, mutate)

    with pytest.raises(ValueError, match="greater than or equal"):
        Config.from_env(config_path=config_path)


def test_runtime_config_loads_verify_dag_concurrency_limits(tmp_path) -> None:
    config_path = _write_runtime_config(tmp_path, lambda _config: None)

    verify = Config.from_env(config_path=config_path).verify

    assert verify.play_max_wall_seconds == 240
    assert verify.max_parallel_producers == 5
    assert verify.max_parallel_provider_producers == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("play_max_wall_seconds", 0),
        ("play_max_wall_seconds", 29),
        ("play_max_wall_seconds", 1.5),
        ("max_parallel_producers", 0),
        ("max_parallel_producers", 1.5),
        ("max_parallel_provider_producers", 0),
        ("max_parallel_provider_producers", True),
    ],
)
def test_runtime_config_rejects_invalid_verify_dag_concurrency(
    tmp_path,
    field: str,
    value,
) -> None:
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["verify"].__setitem__(field, value),
    )

    with pytest.raises(ValueError, match=field):
        Config.from_env(config_path=config_path)


def test_runtime_config_reserves_outer_timeout_for_play_watchdog(tmp_path) -> None:
    def mutate(config) -> None:
        config["verify"]["play_max_wall_seconds"] = 300
        config["verify"]["play_game_timeout_seconds"] = 314

    config_path = _write_runtime_config(tmp_path, mutate)

    with pytest.raises(ValueError, match="play_game_timeout_seconds"):
        Config.from_env(config_path=config_path)


@pytest.mark.parametrize("section", ["image", "sprite"])
@pytest.mark.parametrize("value", ["#FFFFFF", "magenta", 123, None])
def test_runtime_config_rejects_unknown_safe_background_color(
    tmp_path,
    section: str,
    value,
) -> None:
    config_path = _write_runtime_config(
        tmp_path,
        lambda config: config["assets"][section].__setitem__("safe_bg_color", value),
    )

    with pytest.raises(ValueError, match=rf"assets\.{section}\.safe_bg_color"):
        Config.from_env(config_path=config_path)
