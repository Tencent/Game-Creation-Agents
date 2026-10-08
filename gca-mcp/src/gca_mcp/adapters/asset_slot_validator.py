"""Asset slot-fit validator — strict VLM check that a *generated* asset
matches its **design slot** (GDD §2 catalog description), not the
generation prompt.

Why this is distinct from ``asset_image_validator`` / ``asset_video_validator``:
- Those validate an asset *in isolation* against the description that was
  fed into generation (``character_description`` / ``action_description``).
  An asset can clear all of I1..I8 / R1..R10 and still be the WRONG asset
  for the slot — e.g. §2 says ``boss_mech`` is "a hulking four-legged
  walker tank", but the generated sprite is a humanoid robot. Both look
  clean; only a comparison against the *design slot* catches the drift.
- This validator compares the final, packed/matted asset frame against
  the **GDD §2 slot description** for that runtime key. It runs at the
  asset→integration boundary (assets-agent, after ``pack_spritesheet``),
  so packing/matting-introduced drift is caught too.

One pinned Doubao 2.0 request per image, with no provider fallback or retry.
The public tool fans independent slots out concurrently,
reuses hash-valid passed receipts, binds the final runtime artifact, and passes
a tool-owned frame-zero crop here for packed spritesheets.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, NamedTuple

from ..config import Config
from ..errors import ToolError
from .iterate_vlm import DEFAULT_VLM_MODEL, call_vlm_on_image_detailed


class SlotRubric(NamedTuple):
    id: str
    name: str
    description: str


# 5 slot-fit rubrics — every one must PASS for the asset to be considered
# a faithful realization of its GDD §2 design slot.
CANONICAL_SLOT_RUBRIC_VERSION = "asset-slot-rubric/v1"
DEFAULT_SLOT_RUBRICS: tuple[SlotRubric, ...] = (
    SlotRubric(
        "S1", "Role / identity match",
        "The asset must depict the SAME entity/object the SLOT "
        "DESCRIPTION specifies — correct kind (character vs creature vs "
        "vehicle vs prop vs projectile vs effect), correct species/type, "
        "and correct overall form. A different subject than the slot "
        "describes (e.g. slot says 'four-legged walker tank' but the "
        "image shows a humanoid robot) is a FAIL even if the image is "
        "otherwise clean.",
    ),
    SlotRubric(
        "S2", "Key visual features present",
        "The distinctive named features in the SLOT DESCRIPTION must be "
        "visibly present — signature equipment/weapon, body parts, "
        "silhouette cues, and any explicitly described accessories. If "
        "the slot names a feature (e.g. 'twin shoulder cannons', 'red "
        "scarf', 'glowing core') and it is absent or replaced, FAIL.",
    ),
    SlotRubric(
        "S3", "Style & palette consistency",
        "The art style and dominant color palette must match what the "
        "SLOT DESCRIPTION (and any art-direction notes in it) calls for "
        "— e.g. 'cel-shaded with thick black outlines, teal-and-orange "
        "palette'. A clearly different rendering style or a palette that "
        "contradicts the description is a FAIL.",
    ),
    SlotRubric(
        "S4", "Role readability",
        "The asset must read as its gameplay ROLE at a glance, "
        "consistent with the slot's stated purpose: a boss looks "
        "imposing/large-framed; a pickup/item looks like a collectible "
        "object; a projectile reads as a directional projectile; a UI "
        "element looks like an interface piece. If the asset would be "
        "mistaken for a different role in-game, FAIL.",
    ),
    SlotRubric(
        "S5", "No wrong / extra content",
        "Exactly the slot's subject — nothing else. NO baked-in scene "
        "background, NO unrelated secondary subjects, NO captions / text "
        "/ watermark / UI overlays, NO pose-variation grid (unless the "
        "slot explicitly is a tile/prop pack). The subject count and "
        "framing match what the slot implies.",
    ),
)
CANONICAL_SLOT_RUBRIC_SHA256 = hashlib.sha256(
    json.dumps(
        [
            {
                "id": rubric.id,
                "name": rubric.name,
                "description": rubric.description,
            }
            for rubric in DEFAULT_SLOT_RUBRICS
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def _format_rubric_block(rubrics: tuple[SlotRubric, ...]) -> str:
    return "\n".join(f"{r.id}. {r.name} — {r.description}" for r in rubrics)


def _build_output_example(rubrics: tuple[SlotRubric, ...]) -> str:
    fail_index = 1 if len(rubrics) > 1 else 0
    lines: list[str] = []
    for index, rubric in enumerate(rubrics):
        verdict = "FAIL" if index == fail_index else "PASS"
        lines.append(f"RUBRIC {rubric.id}: {verdict}")
        if verdict == "FAIL":
            lines.append("  evidence: <one-sentence concrete mismatch>")
    failed_id = rubrics[fail_index].id
    lines.extend(
        ["", "OVERALL VERDICT: FAIL", f"FAILED RUBRICS: {failed_id}"]
    )
    return "\n".join(lines)


def _build_check_prompt(
    rubrics: tuple[SlotRubric, ...],
    slot_description: str,
    slot_key: str | None,
) -> str:
    rubric_block = _format_rubric_block(rubrics)
    output_example = _build_output_example(rubrics)
    key_line = f"\nRUNTIME KEY: {slot_key}\n" if slot_key else "\n"
    return f"""You are a strict QA reviewer checking whether a generated game asset \
faithfully realizes its DESIGN SLOT. The SLOT DESCRIPTION below is the \
authored design intent (from the game design doc) for this exact asset \
slot — judge the attached image against it. Mark a rubric FAIL only if \
you observe direct visual evidence that the image deviates from the \
SLOT DESCRIPTION; otherwise PASS. Be concrete: quote the specific \
mismatch and where it appears.
{key_line}
SLOT DESCRIPTION (the design intent this asset must match):
{slot_description.strip()}

RUBRICS (every rubric must PASS for the asset to match its design slot):
{rubric_block}

Output format (strictly follow — one rubric per line, evidence indented \
under FAIL lines, OVERALL VERDICT and FAILED RUBRICS at the end):
{output_example}

Strict rules:
- Be strict. If in doubt about a rubric, FAIL it.
- Only mark a rubric FAIL with concrete observed evidence (NOT speculation).
- OVERALL VERDICT is PASS only if every rubric is PASS."""


_RUBRIC_LINE = re.compile(r"^RUBRIC\s+([^:\s]+)\s*:\s*(PASS|FAIL)\s*$", re.IGNORECASE)
_EVIDENCE_LINE = re.compile(r"^\s+evidence\s*:\s*(.+)$", re.IGNORECASE)
_OVERALL_LINE = re.compile(r"^OVERALL\s+VERDICT\s*:\s*(PASS|FAIL)\s*$", re.IGNORECASE)
_FAILED_LINE = re.compile(r"^FAILED\s+RUBRICS\s*:\s*(.+)$", re.IGNORECASE)


def _parse_response(
    text: str,
    rubrics: tuple[SlotRubric, ...],
) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {
        r.id: {"id": r.id, "name": r.name, "verdict": None, "evidence": None}
        for r in rubrics
    }
    canon = {rid.upper(): rid for rid in by_id}
    overall: str | None = None
    last_failed_id: str | None = None
    raw_occurrences: list[tuple[str, str]] = []
    unexpected_ids: list[str] = []
    reported_failed_ids: list[str] | None = None
    overall_occurrences = 0
    failed_line_occurrences = 0

    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = _RUBRIC_LINE.match(raw.strip())
        if m:
            rid = canon.get(m.group(1).upper())
            verdict = m.group(2).upper()
            if rid is not None:
                raw_occurrences.append((rid, verdict))
                previous = by_id[rid]["verdict"]
                if previous is None:
                    by_id[rid]["verdict"] = verdict
                elif previous != verdict:
                    by_id[rid]["verdict"] = "FAIL"
                last_failed_id = rid if verdict == "FAIL" else None
            else:
                unexpected_ids.append(m.group(1))
                last_failed_id = None
            continue
        m = _EVIDENCE_LINE.match(raw)
        if m and last_failed_id is not None:
            by_id[last_failed_id]["evidence"] = m.group(1).strip()
            last_failed_id = None
            continue
        m = _OVERALL_LINE.match(raw.strip())
        if m:
            overall_occurrences += 1
            overall = m.group(1).upper()
            continue
        m = _FAILED_LINE.match(raw.strip())
        if m:
            failed_line_occurrences += 1
            value = m.group(1).strip().strip("\"'")
            if value.lower() in {"none", "n/a", "na", "nil", "null", "-"}:
                reported_failed_ids = []
            else:
                reported_failed_ids = [
                    canon.get(token.strip().upper(), token.strip())
                    for token in value.split(",")
                    if token.strip()
                ]
            continue

    rubric_results = list(by_id.values())
    failed = [r for r in rubric_results if r["verdict"] == "FAIL"]
    passed = [r for r in rubric_results if r["verdict"] == "PASS"]
    unknown = [r for r in rubric_results if r["verdict"] is None]
    seen: dict[str, int] = {}
    for rid, _verdict in raw_occurrences:
        seen[rid] = seen.get(rid, 0) + 1
    duplicate_ids = sorted(rid for rid, count in seen.items() if count > 1)
    actual_failed_ids = sorted(r["id"] for r in failed)
    failed_line_mismatch = bool(
        reported_failed_ids is not None
        and sorted(reported_failed_ids) != actual_failed_ids
    )
    aggregate_duplicate = (
        overall_occurrences > 1 or failed_line_occurrences > 1
    )
    aggregate_missing = (
        overall_occurrences == 0 or failed_line_occurrences == 0
    )
    overall_reported = overall

    if overall is None:
        overall = "PASS" if not failed and not unknown else "FAIL"
    strict_passed = bool(
        passed
        and not failed
        and not unknown
        and not duplicate_ids
        and not unexpected_ids
        and not failed_line_mismatch
        and not aggregate_duplicate
        and not aggregate_missing
        and overall != "FAIL"
    )

    return {
        "verdict": overall,
        "passed": strict_passed,
        "overall_verdict_reported": overall_reported,
        "rubric_results": rubric_results,
        "failures": failed,
        "n_passed": len(passed),
        "n_failed": len(failed),
        "n_unknown": len(unknown),
        "duplicate_ids": duplicate_ids,
        "unexpected_ids": sorted(set(unexpected_ids)),
        "reported_failed_ids": reported_failed_ids,
        "failed_line_mismatch": failed_line_mismatch,
        "aggregate_duplicate": aggregate_duplicate,
        "aggregate_missing": aggregate_missing,
    }


def _coerce_rubrics(items: list[dict[str, str]] | None) -> tuple[SlotRubric, ...]:
    if not items:
        return ()
    out: list[SlotRubric] = []
    seen_ids: set[str] = set()
    for d in items:
        if not isinstance(d, dict):
            raise ToolError(
                "INVALID_CONFIG",
                f"rubric entry must be a dict; got {type(d).__name__}",
            )
        for key in ("id", "name", "description"):
            if key not in d or not str(d[key]).strip():
                raise ToolError(
                    "INVALID_CONFIG",
                    f"rubric entry missing or empty `{key}`: {d}",
                )
        rubric_id = str(d["id"]).strip()
        normalized = rubric_id.upper()
        if normalized in seen_ids:
            raise ToolError(
                "INVALID_CONFIG",
                f"duplicate rubric id {rubric_id!r} in rubric set.",
            )
        seen_ids.add(normalized)
        out.append(SlotRubric(rubric_id, str(d["name"]), str(d["description"])))
    return tuple(out)


def validate_asset_slot(
    config: Config,
    *,
    image_path: Path,
    slot_description: str,
    slot_key: str | None = None,
    extra_rubrics: list[dict[str, str]] | None = None,
    vlm_model: str = DEFAULT_VLM_MODEL,
    vlm_max_tokens: int = 1200,
    vlm_temperature: float = 0.0,
    vlm_timeout: float = 90.0,
    attempts_per_model: int = 1,
) -> dict[str, Any]:
    """Run a strict VLM check that ``image_path`` matches its GDD §2 slot.

    ``slot_description`` is the design-intent text for the runtime key
    (the GDD §2 catalog entry), NOT the generation prompt — comparing
    against the slot is what catches "right-looking but wrong asset".
    """
    if not image_path.is_file():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND",
            f"Asset image not found: {image_path}",
        )
    if not slot_description.strip():
        raise ToolError(
            "INVALID_CONFIG",
            "`slot_description` must be non-empty (the GDD §2 slot text).",
        )

    rubrics = DEFAULT_SLOT_RUBRICS
    if extra_rubrics:
        rubrics = rubrics + _coerce_rubrics(extra_rubrics)
    ids = [rubric.id.upper() for rubric in rubrics]
    if len(ids) != len(set(ids)):
        raise ToolError(
            "INVALID_CONFIG",
            "combined canonical + extra rubric IDs must be unique.",
        )
    if not rubrics:
        raise ToolError("INVALID_CONFIG", "rubric set is empty.")

    prompt = _build_check_prompt(rubrics, slot_description, slot_key)
    vlm_call = call_vlm_on_image_detailed(
        config=config,
        image_path=image_path,
        prompt=prompt,
        model=vlm_model,
        max_tokens=vlm_max_tokens,
        temperature=vlm_temperature,
        timeout=vlm_timeout,
        attempts_per_model=attempts_per_model,
    )
    raw = str(vlm_call["text"])

    parsed = _parse_response(raw, rubrics)
    parsed["image"] = str(image_path)
    if slot_key:
        parsed["slot_key"] = slot_key
    parsed["slot_description"] = slot_description
    selected = vlm_call.get("selected") or {}
    parsed["vlm_model"] = selected.get("model") or vlm_model
    parsed["vlm_provider"] = selected.get("provider")
    parsed["vlm_attempts_per_model"] = attempts_per_model
    parsed["vlm_attempt_trace"] = vlm_call.get("attempts", [])
    parsed["vlm_elapsed_ms"] = vlm_call.get("elapsed_ms")
    parsed["n_rubrics"] = len(rubrics)
    parsed["canonical_rubric_version"] = CANONICAL_SLOT_RUBRIC_VERSION
    parsed["canonical_rubric_sha256"] = CANONICAL_SLOT_RUBRIC_SHA256
    parsed["raw_response"] = raw
    return parsed


__all__ = [
    "CANONICAL_SLOT_RUBRIC_SHA256",
    "CANONICAL_SLOT_RUBRIC_VERSION",
    "DEFAULT_SLOT_RUBRICS",
    "SlotRubric",
    "validate_asset_slot",
]
