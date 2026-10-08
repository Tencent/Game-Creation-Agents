"""Asset-video validator — strict VLM rubric check for game animations.

Two rubric groups, judged on different inputs by the orchestrator:

  FRAMING (raw MP4, solid bg present):
    1. in-frame — nothing (limb / weapon / effect) clipped off a canvas
       edge, and
    2. scale-locked — the character does not balloon / shrink because
       Seedance silently "zoomed in" (including a slow cumulative
       creep); the framing matches the first frame. A pose/limb change
       (punch, crouch, jump) is fine — only a UNIFORM scale of the whole
       figure fails.
  CONTENT (matted transparent output that actually ships):
    3. identity preservation, 4. hand-prop attachment,
    5. limb integrity, 6. loop coherence.

Framing is checked on the raw take (the bg is what makes the margins
measurable); content is checked on the matted frames (matting can eat
a limb / weapon edge, so it must be judged on the final output). Both
groups are HARD — a take ships only if it clears every rubric. Each
group is one request to ``doubao-seed-2-0-pro-260215``.

Pass ``extra_rubrics`` to append action-specific checks, or
``rubric_override`` to replace the default set entirely.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

from ..config import Config
from ..errors import ToolError
from .iterate_vlm import DEFAULT_VLM_MODEL, call_vlm_on_video


class Rubric(NamedTuple):
    """One hard constraint for a game animation video."""

    id: str
    name: str
    description: str


# The sprite-animation rubric set is split into two groups that are
# judged on DIFFERENT inputs by ``produce_validated_video``:
#
#   FRAMING_RUBRICS — judged on the RAW Seedance MP4 (solid bg still
#     present). The bg is what makes the margins / framing measurable,
#     so these must be checked before matting.
#       R1 in-frame, R2 scale-locked (no zoom).
#
#   CONTENT_RUBRICS — judged on the MATTED transparent output (the GIF
#     that actually ships). Matting can eat a limb / weapon edge, so
#     these are checked on the final frames, not the raw take.
#       R3 identity, R4 hand-prop attachment, R5 limb integrity,
#       R6 loop coherence, R7 action fidelity.
#
# All seven are HARD constraints — a take ships only if it clears every
# rubric in BOTH groups. The standalone ``validate_asset_video`` tool
# defaults to FRAMING_RUBRICS (== DEFAULT_RUBRICS) so its single-video
# behavior is unchanged; the orchestrator drives the two-pass split.
FRAMING_RUBRICS: tuple[Rubric, ...] = (
    Rubric(
        "R1", "In-frame",
        "No part of the character — body, limb, head, hair, tail, "
        "cape, held weapon/prop, or emitted effect (slash, projectile, "
        "particle, spell) — may cross or be CLIPPED by any of the four "
        "canvas edges in ANY single frame, including peak-motion frames. "
        "IMPORTANT — grounded framing: the character stands ON the ground, "
        "so the feet / lowest body part RESTING ON the bottom edge is "
        "REQUIRED and PASSES — feet touching the bottom edge is never a "
        "violation. What FAILS is any part being CUT OFF / clipped by an "
        "edge, or the body / a limb / weapon / effect crossing the TOP, "
        "LEFT, or RIGHT edge. The "
        "FULL arc / trajectory / extension of every moving part (sword "
        "swing arc, staff sweep, kick or punch extension, dash motion, "
        "projectile path, particle trail) must stay entirely inside the "
        "visible canvas across ALL frames. If at any timestamp a sword "
        "tip, fist, hair tip, cape edge, or any effect is clipped "
        "by the top, bottom, left, or right edge — even by a few pixels "
        "for a single frame — this rubric FAILS (feet naturally resting "
        "on the bottom edge excepted).",
    ),
    Rubric(
        "R2", "Scale locked (no zoom)",
        "The camera is a locked-off static shot. The character's "
        "on-screen SIZE MUST stay essentially identical to the FIRST "
        "frame across the entire video — the character must NOT balloon "
        "(grow) or shrink on screen, and the framing must NOT zoom in, "
        "zoom out, dolly, or recompose. This includes SLOW, GRADUAL "
        "drift: a creep where the character is a little bigger each "
        "second and ends the clip noticeably larger (or smaller) than "
        "it began is a FAIL even though no single frame-to-frame step "
        "looks dramatic — judge the LAST/late frames against the FIRST, "
        "not just neighboring frames. As a rough bar, a change in the "
        "character's overall height of more than ~10-15% from the first "
        "frame at any point is a FAIL. Tell-tale signs of a FAIL: the "
        "character's bounding-box height noticeably larger or smaller "
        "than in the first frame at any point; the empty background "
        "margin around the character shrinking (a zoom-in) or growing "
        "(a zoom-out) relative to the first frame. Compare early, "
        "middle, and late frames against the first frame. "
        "CRUCIAL distinction — motion vs zoom: a punch / kick / lunge / "
        "crouch / jump / knockdown changes the character's POSE and "
        "SILHOUETTE (a limb extends, the body ducks or leaves the "
        "ground, an arm reaches) — that is expected and PASSES. A zoom "
        "instead scales the WHOLE figure UNIFORMLY — head, torso, AND "
        "limbs all grow together in the same proportion while the pose "
        "is otherwise static (e.g. a standing guard / block that simply "
        "gets bigger). If the size change comes from a moving limb or a "
        "changing pose, PASS; if the entire body scales up/down as one "
        "with no corresponding action, FAIL.",
    ),
)

CONTENT_RUBRICS: tuple[Rubric, ...] = (
    Rubric(
        "R3", "Identity preservation",
        "The character must look like the SAME entity in every frame — "
        "same colors, same armor / clothing patterns, same face "
        "features, same body proportions. No drift, no swap, no morphing "
        "across frames.",
    ),
    Rubric(
        "R4", "Hand-prop attachment",
        "Held items (weapons, shields, tools, staves) MUST remain "
        "anchored to the character's hand or body throughout the video. "
        "An item must NEVER float, hover, detach, or appear in mid-air "
        "without a holding hand visible. If the character has a shield, "
        "it stays gripped on the off-hand; if it has a sword, the sword "
        "stays gripped in the main hand.",
    ),
    Rubric(
        "R5", "Limb integrity",
        "No limb may disappear, duplicate, or bend in an anatomically "
        "impossible direction. All four limbs (or the appropriate body "
        "parts for the species) remain visible and connected to the "
        "torso for the entire video. No clipping through the body. "
        "(Note: this is judged on the matted output — a limb erased by "
        "the matte counts as a FAIL just as much as one the model never "
        "drew.)",
    ),
    Rubric(
        "R6", "Loop coherence",
        "If the action description implies a repeating cycle (walk, "
        "idle, run, breathe, hover), the LAST frame must visually match "
        "the FIRST frame so the loop is seamless. For one-shot actions "
        "(death, intro, hit-react, single-attack-then-recover), mark "
        "this rubric PASS (N/A — no loop is expected).",
    ),
    Rubric(
        "R7", "Action fidelity",
        "The motion must visibly perform the requested action description. "
        "A static pose, the wrong action, motion in the wrong direction, or "
        "an animation whose only change is camera/scale drift is a FAIL.",
    ),
)

# Backward-compat: the standalone validator defaults to the framing
# group (unchanged single-video behavior). The two-pass content check
# is driven explicitly by ``produce_validated_video`` via
# ``rubric_override``.
DEFAULT_RUBRICS: tuple[Rubric, ...] = FRAMING_RUBRICS


# ---- Projectile-BODY rubric groups ----------------------------------------
# A projectile body (bullet / arrow / fireball / bolt) is NOT a grounded
# character: it has no feet resting on the ground, no gripping hand, no
# anatomical limbs. The character CONTENT_RUBRICS (R4 hand-prop, R5 limb
# integrity) would FAIL a perfectly good projectile for defects it can't have,
# and R1's "feet on the bottom edge is REQUIRED" framing is wrong for a
# free-flying object. These groups replace the character defaults when the
# caller selects the projectile animation profile.
PROJECTILE_FRAMING_RUBRICS: tuple[Rubric, ...] = (
    Rubric(
        "PR1", "In-frame",
        "No part of the projectile body may cross or be CLIPPED by any of the "
        "four canvas edges in ANY single frame. Unlike a grounded character, "
        "the projectile is a FREE-FLYING object: it must NOT rest on or touch "
        "the bottom edge either. The full body stays inside the visible canvas "
        "with margin across ALL frames. If at any timestamp the body is clipped "
        "by any edge, FAIL. (Under the body/effect separation contract the body should "
        "have no baked motion trail; if an emitted trail/streak IS present it "
        "too must stay inside the canvas.)",
    ),
    Rubric(
        "PR2", "Scale locked (no zoom)",
        "The camera is a locked-off static shot. The projectile's on-screen "
        "SIZE MUST stay essentially identical across the entire video — it "
        "must NOT balloon or shrink, and the framing must NOT zoom / dolly / "
        "recompose. A projectile spinning or its compact attached flame plume "
        "flickering is fine; a "
        "uniform scale of the whole body with no such motion is a FAIL. Judge "
        "late frames against the first.",
    ),
)

PROJECTILE_CONTENT_RUBRICS: tuple[Rubric, ...] = (
    Rubric(
        "PR3", "Identity preservation",
        "The projectile must look like the SAME object in every frame — same "
        "kind, same colors, same shape and art style. No drift, no swap, no "
        "morphing into a different projectile across frames.",
    ),
    Rubric(
        "PR4", "Body integrity",
        "The projectile body stays a single coherent object throughout — it "
        "does not fragment, tear apart, duplicate into several bodies, or "
        "shed disconnected pieces. Under the body/effect separation contract the body "
        "emits no detached/elongated motion trail or particle wake (that is a "
        "runtime effect). A compact feature attached to the body silhouette "
        "(arrow fletching or a fireball's intrinsic flame plume) is allowed; a "
        "wake extending away from it is a FAIL. (Judged on the matted output — "
        "a body chunk erased by the matte counts as a FAIL.)",
    ),
    Rubric(
        "PR5", "Loop coherence",
        "If the action implies a repeating cycle (a spinning bolt, a pulsing "
        "orb, a looping flight), the LAST frame must visually match the FIRST "
        "so the loop is seamless. For one-shot actions, mark PASS (N/A).",
    ),
    Rubric(
        "PR6", "Action fidelity",
        "The projectile body must visibly perform the requested motion "
        "(spin, pulse, hover, directional travel, or other described action). "
        "A static body or motion contradicting the action description is a FAIL.",
    ),
)


def rubrics_as_dicts(rubrics: tuple[Rubric, ...]) -> list[dict[str, str]]:
    """Serialize a rubric tuple to the ``list[{id,name,description}]``
    shape accepted by ``validate_asset_video(rubric_override=...)``."""
    return [
        {"id": r.id, "name": r.name, "description": r.description}
        for r in rubrics
    ]


def _format_rubric_block(rubrics: tuple[Rubric, ...]) -> str:
    return "\n".join(f"{r.id}. {r.name} — {r.description}" for r in rubrics)


def _build_output_example(rubrics: tuple[Rubric, ...]) -> tuple[str, str]:
    """Build the per-rubric output example + FAILED-RUBRICS line from the ACTIVE
    rubric IDs.

    P0 #2: the example MUST use the real IDs of the rubric set in play. The old
    template hard-coded ``RUBRIC R1 / R2 / R3`` and ``FAILED RUBRICS: R<n>``
    even when the active set was the projectile video group (PR1..PR6). A model
    following the literal example emitted ``R1..`` IDs that the parser (keyed on
    the real PR* IDs) dropped as unknown — leaving every projectile rubric
    UNKNOWN, and a retry got the SAME contradictory template. Deriving the
    example from ``rubrics`` keeps prompt and parser in lock-step for any set.
    """
    ids = [r.id for r in rubrics]
    fail_index = 1 if len(ids) > 1 else 0
    lines: list[str] = []
    for i, rid in enumerate(ids):
        if i == fail_index:
            lines.append(f"RUBRIC {rid}: FAIL")
            lines.append(
                "  evidence: <one-sentence concrete description with "
                "timestamp / frame idx>"
            )
        else:
            lines.append(f"RUBRIC {rid}: PASS")
    example = "\n".join(lines)
    failed_id = ids[fail_index]
    failed_line = f'FAILED RUBRICS: {failed_id} (or "none" if all pass)'
    return example, failed_line


def _build_check_prompt(
    rubrics: tuple[Rubric, ...],
    action_description: str,
) -> str:
    """Assemble the VLM check prompt from the rubric set + action desc."""
    rubric_block = _format_rubric_block(rubrics)
    output_example, failed_line = _build_output_example(rubrics)
    return f"""You are a strict QA reviewer for game-asset animation videos. Inspect \
the entire video frame-by-frame for the listed rubrics. Mark a rubric \
FAIL only if you observe direct evidence of violation; otherwise PASS. \
Be concrete in evidence quotes — cite an approximate timestamp or \
frame index, and quote the specific visual problem you saw.

ACTION DESCRIPTION (what this video is supposed to show):
{action_description.strip()}

RUBRICS (every rubric must PASS for the asset to be game-ready):
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


# Permissive rubric-id capture (any non-colon token) so arbitrary custom IDs —
# which the public schema allows — are parsed; unknown IDs are dropped against
# the active ``by_id`` map in :func:`_parse_response`.
_RUBRIC_LINE = re.compile(r"^RUBRIC\s+([^:\s]+)\s*:\s*(PASS|FAIL)\s*$", re.IGNORECASE)
_EVIDENCE_LINE = re.compile(r"^\s+evidence\s*:\s*(.+)$", re.IGNORECASE)
_OVERALL_LINE = re.compile(r"^OVERALL\s+VERDICT\s*:\s*(PASS|FAIL)\s*$", re.IGNORECASE)
_FAILED_LINE = re.compile(r"^FAILED\s+RUBRICS\s*:\s*(.+)$", re.IGNORECASE)


def _parse_response(
    text: str,
    rubrics: tuple[Rubric, ...],
) -> dict[str, Any]:
    """Parse the VLM verdict text into structured per-rubric outcomes.

    Format expected (case-insensitive):
        RUBRIC R1: PASS
        RUBRIC R2: FAIL
          evidence: ...
        ...
        OVERALL VERDICT: PASS|FAIL
    Missing OVERALL VERDICT is derived from per-rubric verdicts.
    """
    by_id: dict[str, dict[str, Any]] = {
        r.id: {"id": r.id, "name": r.name, "verdict": None, "evidence": None}
        for r in rubrics
    }
    # Case-insensitive lookup so a custom/lowercase ID in the reply still maps
    # to its canonical rubric entry.
    canon: dict[str, str] = {rid.upper(): rid for rid in by_id}
    overall: str | None = None
    last_failed_id: str | None = None
    # P1 #7: keep every rubric occurrence so a duplicate output line is detected
    # rather than silently overwritten (a later PASS erasing an earlier FAIL).
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
                if prev is None:
                    by_id[rid]["verdict"] = verdict
                elif prev != verdict:
                    by_id[rid]["verdict"] = "FAIL"  # fail-closed on contradiction
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

    # P1 #7: derive ``passed`` from the COMPLETE, UNIQUE, all-PASS rubric set,
    # never from a possibly-contradictory OVERALL VERDICT line.
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


def _coerce_rubrics(
    items: list[dict[str, str]] | None,
) -> tuple[Rubric, ...]:
    if not items:
        return ()
    out: list[Rubric] = []
    seen_ids: set[str] = set()
    for d in items:
        if not isinstance(d, dict):
            raise ToolError(
                "INVALID_CONFIG",
                f"rubric entry must be an object with id/name/description; got {type(d).__name__}",
            )
        for key in ("id", "name", "description"):
            if key not in d or not str(d[key]).strip():
                raise ToolError(
                    "INVALID_CONFIG",
                    f"rubric entry missing or empty `{key}` field: {d}",
                )
        # P1 #7: reject duplicate rubric IDs before the VLM runs (case-
        # insensitive, matching the parser).
        rid = str(d["id"]).strip()
        key_norm = rid.upper()
        if key_norm in seen_ids:
            raise ToolError(
                "INVALID_CONFIG",
                f"duplicate rubric id {rid!r} in rubric set — each id must be unique.",
            )
        seen_ids.add(key_norm)
        out.append(Rubric(rid, str(d["name"]), str(d["description"])))
    return tuple(out)


def _ensure_unique_rubrics(rubrics: tuple[Rubric, ...]) -> None:
    ids = [rubric.id.upper() for rubric in rubrics]
    if len(ids) != len(set(ids)):
        raise ToolError(
            "INVALID_CONFIG",
            "combined default/override + extra rubric IDs must be unique.",
        )


def validate_asset_video(
    config: Config,
    *,
    video_path: Path,
    action_description: str,
    extra_rubrics: list[dict[str, str]] | None = None,
    rubric_override: list[dict[str, str]] | None = None,
    vlm_model: str = DEFAULT_VLM_MODEL,
    vlm_max_tokens: int = 1500,
    vlm_temperature: float = 0.0,
    vlm_timeout: float = 180.0,
) -> dict[str, Any]:
    """Run a strict VLM rubric check on a generated game-asset video.

    ``rubric_override`` replaces the default 2-rubric set entirely.
    ``extra_rubrics`` appends additional checks to the default set.
    They are mutually compatible: if both are provided, override wins
    and extra_rubrics is appended to that.
    """
    if not video_path.is_file():
        raise ToolError(
            "INPUT_FILE_NOT_FOUND",
            f"Asset video not found: {video_path}",
        )
    if not action_description.strip():
        raise ToolError(
            "INVALID_CONFIG",
            "`action_description` must be a non-empty string describing "
            "what the video is supposed to show.",
        )

    if rubric_override:
        rubrics = _coerce_rubrics(rubric_override)
    else:
        rubrics = DEFAULT_RUBRICS
    if extra_rubrics:
        rubrics = rubrics + _coerce_rubrics(extra_rubrics)
    _ensure_unique_rubrics(rubrics)
    if not rubrics:
        raise ToolError(
            "INVALID_CONFIG",
            "rubric set is empty — provide at least one rubric.",
        )

    prompt = _build_check_prompt(rubrics, action_description)
    raw = call_vlm_on_video(
        config=config,
        video_path=video_path,
        prompt=prompt,
        model=vlm_model,
        max_tokens=vlm_max_tokens,
        temperature=vlm_temperature,
        timeout=vlm_timeout,
    )

    parsed = _parse_response(raw, rubrics)
    parsed["video"] = str(video_path)
    parsed["action_description"] = action_description
    parsed["vlm_model"] = vlm_model
    parsed["n_rubrics"] = len(rubrics)
    parsed["raw_response"] = raw
    return parsed


__all__ = [
    "DEFAULT_RUBRICS",
    "FRAMING_RUBRICS",
    "CONTENT_RUBRICS",
    "PROJECTILE_FRAMING_RUBRICS",
    "PROJECTILE_CONTENT_RUBRICS",
    "Rubric",
    "rubrics_as_dicts",
    "validate_asset_video",
]
