"""Asset-image validator — strict VLM rubric check for sprite first-frames.

Pre-i2v gate: validates a single static PNG (typically a sprite action's
first-frame from ``generate_image``) against 8 single-frame composition
rubrics. Catches defects like "shield rendered floating beside the body
with no visible holding hand" — the kind of static composition issue
that the video-level validator's ``R2 Hand-prop attachment`` misses
(because the prop doesn't *detach mid-animation*; it was never gripped
in the first place).

Single VLM round-trip on ``doubao-seed-2-0-pro-260215``. Run after
``generate_image`` and before ``generate_seedance_video`` / ``build_i2v_prompt``.

Why a separate validator instead of reusing the video one:
- Different rubric set (no R5 loop coherence, no R8 camera lock, no
  R10 effects-solid — those are time-domain rubrics).
- VLM accuracy is meaningfully higher when the prompt asks specifically
  about a single static composition vs. a multi-frame video.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

from ..config import Config
from ..errors import ToolError
from .iterate_vlm import DEFAULT_VLM_MODEL, call_vlm_on_image


class ImageRubric(NamedTuple):
    id: str
    name: str
    description: str


# 8 single-frame composition rubrics — distilled from observed image_gen
# defects (notably the floating-shield bug on the knight first-frame
# that slipped past the video validator and corrupted all 121 i2v
# frames downstream).
DEFAULT_IMAGE_RUBRICS: tuple[ImageRubric, ...] = (
    ImageRubric(
        "I1", "Identity match",
        "The character must literally match the description provided in "
        "the CHARACTER DESCRIPTION block — correct species, gender, era, "
        "art style, and notable visual features. No substitutions or "
        "embellishments outside the description.",
    ),
    ImageRubric(
        "I2", "Hand-prop visible attachment",
        "Every prop in the image (weapon, shield, tool, staff, etc.) MUST "
        "have a clearly VISIBLE holding hand or forearm directly on the "
        "prop — fingers, knuckles, or wrist must be rendered actually "
        "gripping it. A prop appearing 'near' the body or 'beside' the "
        "torso without a visible grip is a FAIL even if the prop is in "
        "a plausible position. If the character holds two props, BOTH "
        "must show their gripping hand/forearm.",
    ),
    ImageRubric(
        "I3", "Limb integrity",
        "Every expected limb (arms, legs, or species-equivalent body "
        "parts) must be visibly rendered, anatomically connected to the "
        "torso, with realistic joint angles. NO missing limbs, NO extra "
        "limbs, NO duplicated limbs, NO impossible bends, NO limbs "
        "clipping through the body.",
    ),
    ImageRubric(
        "I4", "Foot anchor",
        "The character's lowest visible point (feet, paws, hooves, base, "
        "wheels) must be in the LOWER PORTION of the canvas — within the "
        "bottom 30% of the canvas height. (Equivalent: the lowest opaque "
        "pixel's y-coordinate should be in the bottom 30% of the image.) "
        "FAIL only if the feet are clearly above this line (vertical "
        "centering, character floating in mid-canvas, character "
        "occupying the upper half). A small margin of empty space below "
        "the feet is acceptable — downstream matting + frame alignment "
        "will pull the lowest opaque pixel down to the canvas bottom.",
    ),
    ImageRubric(
        "I5", "In-frame",
        "The entire character, including held props, accessories, hair, "
        "tail, and cape, fits inside the canvas without clipping at any "
        "of the four edges. Furthermore, there must be sufficient empty "
        "background margin on all four sides for downstream i2v motion: "
        "≥20% empty bg above the head (for swings / spells / jumps), "
        "and ≥15% empty bg on each of the left and right sides (for "
        "swing arc, sweep, dash, and projectile trails). If any held "
        "weapon tip, prop, hair tip, or accessory sits within the "
        "leftmost / rightmost / top 5% of the canvas — even without "
        "actually clipping the edge — this rubric FAILS, because "
        "downstream i2v motion will then push that part off-screen.",
    ),
    ImageRubric(
        "I6", "Background pure",
        "The background is a uniform, pure, solid color (typically the "
        "requested chroma-key bg color such as #FF00FF or #00F000). NO "
        "gradient, NO shading, NO noise, NO secondary objects, NO "
        "decorative elements, NO border.",
    ),
    ImageRubric(
        "I7", "Single character",
        "Exactly ONE full-body character occupies the canvas. NO duplicate "
        "/ secondary characters, NO thumbnails or pose-variation grids, "
        "NO UI overlays, NO captions or text, NO border frames.",
    ),
    ImageRubric(
        "I8", "View consistency",
        "The view angle matches the description (side / 3-quarter / "
        "front / back). The character is not rotated or shown from a "
        "different perspective than requested.",
    ),
)


# Projectile-BODY first-frame rubrics. A projectile body (bullet / arrow /
# fireball / bolt) is NOT a grounded character: it has no feet, no gripping
# hand, no anatomical limbs, and it should be CENTERED with margin (not glued
# to the bottom edge). Reusing DEFAULT_IMAGE_RUBRICS would fail it on I2
# (hand-prop), I3 (limb integrity), and I4 (foot anchor) for defects it can't
# have. This set keeps identity / in-frame / single-subject / view and adds a
# body-integrity + travel-direction check appropriate to a projectile.
PROJECTILE_IMAGE_RUBRICS: tuple[ImageRubric, ...] = (
    ImageRubric(
        "P1", "Identity match",
        "The projectile body must match the description provided in the "
        "CHARACTER DESCRIPTION block — correct kind (bullet / arrow / "
        "fireball / bolt / orb), shape, and art style. No substitutions.",
    ),
    ImageRubric(
        "P2", "Body integrity",
        "The projectile is a single coherent object — no duplicated copies, "
        "no fragmented / torn body, no stray disconnected pieces. It reads as "
        "ONE projectile, not a burst of several. Under the body/effect separation "
        "contract the body must NOT emit a detached/elongated motion trail, "
        "comet streak, or particle wake — those are runtime ParticleEmitter "
        "effects. A compact feature physically attached to the body silhouette "
        "(arrow fletching or a fireball's small intrinsic flame plume) is part "
        "of the body and is allowed; a wake extending away from it is a FAIL.",
    ),
    ImageRubric(
        "P3", "Centered with margin",
        "The projectile body is CENTERED in the canvas with clear empty "
        "background margin on ALL FOUR edges (≥8% each side). It must NOT be "
        "glued to the bottom edge and must NOT touch or clip any edge — a "
        "projectile is a free-flying object, not a grounded figure. FAIL if "
        "the body sits within 5% of any edge.",
    ),
    ImageRubric(
        "P4", "Travel direction",
        "If the description specifies a facing / travel direction (e.g. "
        "'fires toward the right', 'toward the player on the left'), the "
        "projectile's orientation matches it. For a symmetric/non-directional "
        "body, mark PASS (N/A).",
    ),
    ImageRubric(
        "P5", "Background pure",
        "The background is a uniform, pure, solid color (the requested "
        "chroma-key bg such as #FF00FF / #00F000). NO gradient, NO shading, "
        "NO secondary objects, NO border.",
    ),
    ImageRubric(
        "P6", "Single object",
        "Exactly ONE projectile body occupies the canvas. NO duplicate / "
        "secondary projectiles, NO thumbnails or variation grids, NO UI "
        "overlays, NO captions or text, NO border frames.",
    ),
)


def rubric_dicts(rubrics: tuple[ImageRubric, ...]) -> list[dict[str, str]]:
    """Serialize a rubric tuple to the ``list[{id,name,description}]`` shape
    accepted by ``validate_image(rubric_override=...)``."""
    return [
        {"id": r.id, "name": r.name, "description": r.description}
        for r in rubrics
    ]


def _format_rubric_block(rubrics: tuple[ImageRubric, ...]) -> str:
    return "\n".join(f"{r.id}. {r.name} — {r.description}" for r in rubrics)


def _build_output_example(rubrics: tuple[ImageRubric, ...]) -> tuple[str, str]:
    """Build the per-rubric output example + FAILED-RUBRICS line from the ACTIVE
    rubric IDs.

    P0 #2: the example MUST use the real IDs of the rubric set in play. The old
    template hard-coded ``RUBRIC I1 / I2 / I3`` and ``FAILED RUBRICS: I<n>``
    even when the active set was the projectile group (P1..P6). A model that
    followed the literal example then emitted ``I1..`` IDs that the parser
    (keyed on the real P* IDs) dropped as unknown — leaving every projectile
    rubric UNKNOWN, so a retry got the SAME contradictory template and never
    converged. Deriving the example from ``rubrics`` keeps prompt and parser in
    lock-step for ANY rubric set (defaults, projectile, or custom).
    """
    ids = [r.id for r in rubrics]
    fail_index = 1 if len(ids) > 1 else 0
    lines: list[str] = []
    for i, rid in enumerate(ids):
        # Show one FAIL-with-evidence example on the 2nd rubric (if any) so the
        # evidence-line shape is demonstrated; everything else PASS.
        if i == fail_index:
            lines.append(f"RUBRIC {rid}: FAIL")
            lines.append("  evidence: <one-sentence concrete description>")
        else:
            lines.append(f"RUBRIC {rid}: PASS")
    example = "\n".join(lines)
    failed_id = ids[fail_index]
    failed_line = f'FAILED RUBRICS: {failed_id} (or "none" if all pass)'
    return example, failed_line


def _build_check_prompt(
    rubrics: tuple[ImageRubric, ...],
    character_description: str,
) -> str:
    rubric_block = _format_rubric_block(rubrics)
    output_example, failed_line = _build_output_example(rubrics)
    return f"""You are a strict QA reviewer for game-asset sprite first-frames. \
Inspect the single image attached for the listed rubrics. Mark a rubric \
FAIL only if you observe direct visual evidence of violation; otherwise \
PASS. Be concrete in evidence — describe the specific visual problem \
and where it appears in the image (e.g. "the shield occupies the right \
side of the torso but no hand or forearm is visible touching it").

CHARACTER DESCRIPTION (what the image is supposed to show):
{character_description.strip()}

RUBRICS (every rubric must PASS for the first-frame to be ready for \
i2v animation):
{rubric_block}

Output format (strictly follow — one rubric per line, use EXACTLY the \
rubric IDs listed above, evidence indented under FAIL lines, OVERALL \
VERDICT and FAILED RUBRICS at the end):
{output_example}
... (every rubric on its own line, in the order listed above)

OVERALL VERDICT: PASS or FAIL
{failed_line}

Strict rules:
- Be strict. If in doubt about a rubric, FAIL it.
- Only mark a rubric FAIL with concrete observed evidence (NOT speculation).
- Use the EXACT rubric IDs shown above — do not renumber or relabel them.
- OVERALL VERDICT is PASS only if every rubric is PASS."""


# The rubric-id capture is intentionally permissive (any non-colon token) so
# custom rubric IDs — which the public schema allows to be arbitrary non-empty
# strings, not just ``[A-Za-z]+\d+`` — are parsed too; unknown IDs are dropped
# against the active ``by_id`` map in :func:`_parse_response`.
_RUBRIC_LINE = re.compile(r"^RUBRIC\s+([^:\s]+)\s*:\s*(PASS|FAIL)\s*$", re.IGNORECASE)
_EVIDENCE_LINE = re.compile(r"^\s+evidence\s*:\s*(.+)$", re.IGNORECASE)
_OVERALL_LINE = re.compile(r"^OVERALL\s+VERDICT\s*:\s*(PASS|FAIL)\s*$", re.IGNORECASE)
_FAILED_LINE = re.compile(r"^FAILED\s+RUBRICS\s*:\s*(.+)$", re.IGNORECASE)


def _parse_response(
    text: str,
    rubrics: tuple[ImageRubric, ...],
) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {
        r.id: {"id": r.id, "name": r.name, "verdict": None, "evidence": None}
        for r in rubrics
    }
    # Case-insensitive lookup so a custom/lowercase ID in the VLM reply still
    # maps to its canonical rubric entry (the ID capture is now permissive).
    canon: dict[str, str] = {rid.upper(): rid for rid in by_id}
    overall: str | None = None
    last_failed_id: str | None = None
    # P1 #7: preserve EVERY rubric occurrence (in order) so a duplicate output
    # line for one ID is detected instead of silently overwritten — a later
    # ``PASS`` must never erase an earlier ``FAIL`` for the same rubric.
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
                prev = by_id[rid]["verdict"]
                # First occurrence sets the verdict; a conflicting later line
                # cannot upgrade a FAIL to PASS (fail-closed on contradiction).
                if prev is None:
                    by_id[rid]["verdict"] = verdict
                elif prev != verdict:
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
            if value.lower() == "none":
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

    # A rubric ID that appeared on more than one output line is a duplicate.
    seen: dict[str, int] = {}
    for rid, _v in raw_occurrences:
        seen[rid] = seen.get(rid, 0) + 1
    duplicate_ids = sorted(rid for rid, n in seen.items() if n > 1)
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

    # P1 #7: derive ``passed`` from the COMPLETE, UNIQUE, all-PASS rubric set —
    # never trust an OVERALL VERDICT line that contradicts the per-rubric
    # results (a reply with a FAIL/UNKNOWN plus "OVERALL VERDICT: PASS" must
    # NOT pass). The OVERALL line is retained for reporting only.
    strict_passed = (
        not failed
        and not unknown
        and not duplicate_ids
        and not unexpected_ids
        and not failed_line_mismatch
        and not aggregate_duplicate
        and not aggregate_missing
        and overall != "FAIL"
        and bool(passed)
    )
    if overall is None:
        overall = "PASS" if strict_passed else "FAIL"

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


def _coerce_rubrics(items: list[dict[str, str]] | None) -> tuple[ImageRubric, ...]:
    if not items:
        return ()
    out: list[ImageRubric] = []
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
        # P1 #7: reject duplicate rubric IDs BEFORE the VLM runs — two rubrics
        # sharing an ID collapse to one dict key downstream and one silently
        # shadows the other. IDs are compared case-insensitively because the
        # parser matches them that way.
        rid = str(d["id"]).strip()
        key_norm = rid.upper()
        if key_norm in seen_ids:
            raise ToolError(
                "INVALID_CONFIG",
                f"duplicate rubric id {rid!r} in rubric set — each id must be unique.",
            )
        seen_ids.add(key_norm)
        out.append(ImageRubric(rid, str(d["name"]), str(d["description"])))
    return tuple(out)


def _ensure_unique_rubrics(rubrics: tuple[ImageRubric, ...]) -> None:
    ids = [rubric.id.upper() for rubric in rubrics]
    if len(ids) != len(set(ids)):
        raise ToolError(
            "INVALID_CONFIG",
            "combined default/override + extra rubric IDs must be unique.",
        )


def validate_image(
    config: Config,
    *,
    image_path: Path,
    character_description: str,
    extra_rubrics: list[dict[str, str]] | None = None,
    rubric_override: list[dict[str, str]] | None = None,
    vlm_model: str = DEFAULT_VLM_MODEL,
    vlm_max_tokens: int = 1200,
    vlm_temperature: float = 0.0,
    vlm_timeout: float = 90.0,
) -> dict[str, Any]:
    """Run a strict VLM rubric check on a sprite first-frame image."""
    if not image_path.is_file():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND",
            f"First-frame image not found: {image_path}",
        )
    if not character_description.strip():
        raise ToolError(
            "INVALID_CONFIG",
            "`character_description` must be non-empty.",
        )

    if rubric_override:
        rubrics = _coerce_rubrics(rubric_override)
    else:
        rubrics = DEFAULT_IMAGE_RUBRICS
    if extra_rubrics:
        rubrics = rubrics + _coerce_rubrics(extra_rubrics)
    _ensure_unique_rubrics(rubrics)
    if not rubrics:
        raise ToolError(
            "INVALID_CONFIG", "rubric set is empty.",
        )

    prompt = _build_check_prompt(rubrics, character_description)
    raw = call_vlm_on_image(
        config=config,
        image_path=image_path,
        prompt=prompt,
        model=vlm_model,
        max_tokens=vlm_max_tokens,
        temperature=vlm_temperature,
        timeout=vlm_timeout,
    )

    parsed = _parse_response(raw, rubrics)
    parsed["image"] = str(image_path)
    parsed["character_description"] = character_description
    parsed["vlm_model"] = vlm_model
    parsed["n_rubrics"] = len(rubrics)
    parsed["raw_response"] = raw
    return parsed


__all__ = [
    "DEFAULT_IMAGE_RUBRICS",
    "PROJECTILE_IMAGE_RUBRICS",
    "ImageRubric",
    "rubric_dicts",
    "validate_image",
]
