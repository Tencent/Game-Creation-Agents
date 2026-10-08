"""Regression tests for the strict VLM rubric validators.

Covers PR #123 review:
  #2 — validator output examples must use the ACTIVE rubric IDs (projectile
       P*/PR* sets, not the hard-coded I*/R*), and the parser must accept
       arbitrary custom IDs.
  #7 — standalone `passed` derived from the strict rubric set (never a
       contradictory OVERALL VERDICT), duplicate output lines detected, and
       duplicate rubric DEFINITIONS rejected before the VLM runs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gca_mcp import providers
from gca_mcp.adapters import asset_image_validator as aiv
from gca_mcp.adapters import asset_slot_validator as asv
from gca_mcp.adapters import asset_video_validator as avv
from gca_mcp.adapters import iterate_vlm
from gca_mcp.errors import ToolError
from gca_mcp.providers import VlmAttemptError

# -------------------- #2: dynamic output example uses active IDs --------------------


def test_image_check_prompt_uses_projectile_ids():
    prompt = aiv._build_check_prompt(aiv.PROJECTILE_IMAGE_RUBRICS, "a fireball")
    # The output example must reference P* IDs, never the default I* IDs.
    assert "RUBRIC P1:" in prompt
    assert "RUBRIC I1:" not in prompt
    # The example marks its second rubric FAIL, so the summary must agree.
    assert "RUBRIC P2: FAIL" in prompt
    assert "FAILED RUBRICS: P2" in prompt


def test_video_check_prompt_uses_projectile_ids():
    prompt = avv._build_check_prompt(avv.PROJECTILE_FRAMING_RUBRICS, "a fireball flies")
    assert "RUBRIC PR1:" in prompt
    assert "RUBRIC R1:" not in prompt


def test_parser_accepts_custom_ids():
    rubrics = (aiv.ImageRubric("custom_x", "Custom", "desc"),)
    parsed = aiv._parse_response(
        "RUBRIC custom_x: PASS\nOVERALL VERDICT: PASS\n"
        "FAILED RUBRICS: none",
        rubrics,
    )
    assert parsed["passed"] is True
    assert parsed["n_passed"] == 1


def test_projectile_ids_parse_end_to_end():
    # A model that follows the (now correct) example emits P* IDs that the
    # parser keyed on the P* set actually records — no phantom UNKNOWNs.
    rubrics = aiv.PROJECTILE_IMAGE_RUBRICS
    lines = [f"RUBRIC {r.id}: PASS" for r in rubrics] + [
        "OVERALL VERDICT: PASS",
        "FAILED RUBRICS: none",
    ]
    parsed = aiv._parse_response("\n".join(lines), rubrics)
    assert parsed["passed"] is True
    assert parsed["n_unknown"] == 0


# -------------------- #7: strict passed / duplicate detection --------------------


def test_contradictory_overall_verdict_does_not_pass():
    # A per-rubric FAIL with "OVERALL VERDICT: PASS" must NOT pass.
    rubrics = aiv.DEFAULT_IMAGE_RUBRICS
    lines = [f"RUBRIC {r.id}: PASS" for r in rubrics]
    lines[1] = f"RUBRIC {rubrics[1].id}: FAIL"
    lines.append("OVERALL VERDICT: PASS")  # lies
    parsed = aiv._parse_response("\n".join(lines), rubrics)
    assert parsed["passed"] is False
    assert parsed["overall_verdict_reported"] == "PASS"


def test_explicit_overall_fail_blocks_all_pass_rubrics():
    rubrics = (aiv.ImageRubric("I1", "Identity", "desc"),)
    parsed = aiv._parse_response(
        "RUBRIC I1: PASS\nOVERALL VERDICT: FAIL\nFAILED RUBRICS: none",
        rubrics,
    )
    assert parsed["passed"] is False


def test_duplicate_aggregate_lines_fail_closed():
    rubrics = (aiv.ImageRubric("I1", "Identity", "desc"),)
    parsed = aiv._parse_response(
        "RUBRIC I1: PASS\nOVERALL VERDICT: FAIL\n"
        "OVERALL VERDICT: PASS\nFAILED RUBRICS: none",
        rubrics,
    )
    assert parsed["aggregate_duplicate"] is True
    assert parsed["passed"] is False


def test_missing_aggregate_lines_fail_closed():
    rubrics = (aiv.ImageRubric("I1", "Identity", "desc"),)
    parsed = aiv._parse_response("RUBRIC I1: PASS", rubrics)
    assert parsed["aggregate_missing"] is True
    assert parsed["overall_verdict_reported"] is None
    assert parsed["passed"] is False


def test_quoted_none_failed_summary_is_accepted():
    rubrics = (aiv.ImageRubric("I1", "Identity", "desc"),)
    parsed = aiv._parse_response(
        'RUBRIC I1: PASS\nOVERALL VERDICT: PASS\nFAILED RUBRICS: "none"',
        rubrics,
    )
    assert parsed["aggregate_missing"] is False
    assert parsed["reported_failed_ids"] == []
    assert parsed["passed"] is True


def test_single_rubric_output_example_is_self_consistent():
    rubrics = (aiv.ImageRubric("only_one", "Only", "desc"),)
    prompt = aiv._build_check_prompt(rubrics, "single asset")
    assert "RUBRIC only_one: FAIL" in prompt
    assert "FAILED RUBRICS: only_one" in prompt


def test_failed_rubrics_summary_must_match_lines():
    rubrics = (
        aiv.ImageRubric("I1", "One", "desc"),
        aiv.ImageRubric("I2", "Two", "desc"),
    )
    parsed = aiv._parse_response(
        "RUBRIC I1: PASS\nRUBRIC I2: PASS\n"
        "OVERALL VERDICT: PASS\nFAILED RUBRICS: I2",
        rubrics,
    )
    assert parsed["failed_line_mismatch"] is True
    assert parsed["passed"] is False


def test_duplicate_output_line_fails_closed():
    # A later PASS must not erase an earlier FAIL for the same ID; the duplicate
    # is detected and the set is not a clean pass.
    rubrics = (aiv.ImageRubric("I1", "Identity", "desc"),)
    text = "RUBRIC I1: FAIL\n  evidence: bad\nRUBRIC I1: PASS\nOVERALL VERDICT: PASS"
    parsed = aiv._parse_response(text, rubrics)
    assert "I1" in parsed["duplicate_ids"]
    assert parsed["passed"] is False


def test_duplicate_all_pass_lines_still_fail_closed():
    rubrics = (aiv.ImageRubric("I1", "Identity", "desc"),)
    parsed = aiv._parse_response(
        "RUBRIC I1: PASS\nRUBRIC I1: PASS\nOVERALL VERDICT: PASS",
        rubrics,
    )
    assert parsed["rubric_results"][0]["verdict"] == "PASS"
    assert parsed["duplicate_ids"] == ["I1"]
    assert parsed["passed"] is False


def test_unexpected_output_id_fails_closed():
    rubrics = (aiv.ImageRubric("I1", "Identity", "desc"),)
    parsed = aiv._parse_response(
        "RUBRIC I1: PASS\nRUBRIC EXTRA: PASS\nOVERALL VERDICT: PASS",
        rubrics,
    )
    assert parsed["unexpected_ids"] == ["EXTRA"]
    assert parsed["passed"] is False


def test_duplicate_rubric_definition_rejected_image():
    with pytest.raises(ToolError):
        aiv._coerce_rubrics(
            [
                {"id": "X1", "name": "a", "description": "d"},
                {"id": "X1", "name": "b", "description": "e"},
            ]
        )


def test_duplicate_rubric_definition_rejected_video():
    with pytest.raises(ToolError):
        avv._coerce_rubrics(
            [
                {"id": "X1", "name": "a", "description": "d"},
                {"id": "x1", "name": "b", "description": "e"},  # case-insensitive
            ]
        )


def test_combined_default_and_extra_duplicate_is_rejected():
    with pytest.raises(ToolError):
        aiv._ensure_unique_rubrics(
            aiv.DEFAULT_IMAGE_RUBRICS
            + (aiv.ImageRubric("i1", "duplicate", "desc"),)
        )


def test_slot_validator_ignores_lying_overall_pass():
    rubrics = asv.DEFAULT_SLOT_RUBRICS
    lines = [f"RUBRIC {rubric.id}: PASS" for rubric in rubrics]
    lines[0] = "RUBRIC S1: FAIL"
    lines.append("OVERALL VERDICT: PASS")
    parsed = asv._parse_response("\n".join(lines), rubrics)
    assert parsed["passed"] is False
    assert parsed["overall_verdict_reported"] == "PASS"


def test_slot_validator_respects_explicit_overall_fail():
    rubrics = (asv.SlotRubric("S1", "One", "desc"),)
    parsed = asv._parse_response(
        "RUBRIC S1: PASS\nOVERALL VERDICT: FAIL\nFAILED RUBRICS: none",
        rubrics,
    )
    assert parsed["passed"] is False


def test_slot_prompt_and_parser_support_custom_ids():
    rubrics = (asv.SlotRubric("custom_slot", "Custom", "desc"),)
    prompt = asv._build_check_prompt(rubrics, "slot", None)
    assert "RUBRIC custom_slot:" in prompt
    parsed = asv._parse_response(
        "RUBRIC custom_slot: PASS\nOVERALL VERDICT: PASS\n"
        "FAILED RUBRICS: none",
        rubrics,
    )
    assert parsed["passed"] is True


def test_slot_parser_treats_na_failed_summary_as_empty():
    rubrics = (asv.SlotRubric("S1", "One", "desc"),)
    parsed = asv._parse_response(
        "RUBRIC S1: PASS\nOVERALL VERDICT: PASS\nFAILED RUBRICS: N/A",
        rubrics,
    )

    assert parsed["reported_failed_ids"] == []
    assert parsed["failed_line_mismatch"] is False
    assert parsed["passed"] is True


def test_vlm_failure_stops_after_one_pinned_request(
    monkeypatch,
):
    endpoint = SimpleNamespace(
        provider="ark-doubao",
        bare_model="doubao-seed-2-0-pro-260215",
    )
    calls = []

    class FakeProvider:
        @staticmethod
        def vlm_chain(_config):
            return [endpoint]

        @staticmethod
        def vlm_retry(_config):
            return (1, 0.0, 0.0)

        @staticmethod
        def vlm_attempt(_config, endpoint, **_kwargs):
            calls.append(endpoint.bare_model)
            raise VlmAttemptError(
                "HTTP 400: invalid request",
                retryable=False,
                status_code=400,
            )

    monkeypatch.setattr(iterate_vlm, "get_provider", lambda _config: FakeProvider())
    with pytest.raises(ToolError, match="Pinned Doubao 2.0"):
        iterate_vlm._post_vlm_detailed(
            config=SimpleNamespace(),
            content=[],
            max_tokens=100,
            temperature=0.0,
            timeout=1.0,
        )

    assert calls == ["doubao-seed-2-0-pro-260215"]


def test_vlm_policy_rejects_multiple_endpoints(monkeypatch):
    endpoints = [
        SimpleNamespace(provider="ark-doubao", bare_model="first"),
        SimpleNamespace(provider="ark-doubao", bare_model="fast"),
    ]
    calls = []

    class FakeProvider:
        @staticmethod
        def vlm_chain(_config):
            return endpoints

        @staticmethod
        def vlm_retry(_config):
            return (1, 0.0, 0.0)

        @staticmethod
        def vlm_attempt(_config, endpoint, **_kwargs):
            calls.append(endpoint.bare_model)
            return "unexpected"

    monkeypatch.setattr(iterate_vlm, "get_provider", lambda _config: FakeProvider())
    with pytest.raises(ToolError, match="exactly one configured model"):
        iterate_vlm._post_vlm_detailed(
            config=SimpleNamespace(),
            content=[],
            max_tokens=100,
            temperature=0.0,
            timeout=1.0,
            attempts_per_model=1,
        )

    assert calls == []


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(400, False), (401, False), (404, False), (429, True), (503, True)],
)
def test_vlm_http_status_retry_classification(
    monkeypatch,
    status_code,
    retryable,
):
    response = SimpleNamespace(
        status_code=status_code,
        text="rate limit" if status_code == 429 else "failure",
    )
    monkeypatch.setattr(
        providers.requests,
        "post",
        lambda *_args, **_kwargs: response,
    )

    with pytest.raises(VlmAttemptError) as exc_info:
        providers.vlm_roundtrip(
            "https://example.invalid",
            {},
            {},
            1.0,
        )

    assert exc_info.value.retryable is retryable
    assert exc_info.value.status_code == status_code


def test_action_fidelity_rubrics_are_mandatory():
    assert "R7" in {rubric.id for rubric in avv.CONTENT_RUBRICS}
    assert "PR6" in {
        rubric.id for rubric in avv.PROJECTILE_CONTENT_RUBRICS
    }
