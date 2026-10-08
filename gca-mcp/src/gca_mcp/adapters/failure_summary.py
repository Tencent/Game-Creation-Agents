"""Shared failure summarisation for retry orchestrators.

Both ``produce_validated_video`` (video VLM rubrics) and
``produce_validated_first_frame`` (image VLM rubrics) call this when
folding prior attempt failures into the next attempt's prompt.

Two design choices that matter:

1. **Group by rubric ID, not by attempt.** Naively appending one
   ``- previous attempt failed because: <evidence>`` line per failure
   produces several near-identical lines (e.g. three different
   timestamps of the same R7 sword-tip-off-screen issue) which the
   model mostly ignores as repetitive noise. Grouping the evidence
   under one rubric block makes the *pattern* visible and lets us
   attach a single, sharper instruction.

2. **Append rubric-specific generalised guidance.** Per-rubric
   guidance (eg. "reduce swing amplitude" for R7) is hard-coded
   below — it converts the validator's *observation* into an
   *actionable instruction* for the generator. Without this, the
   generator only knows what NOT to do; with it, it also knows what
   TO do differently.

Public API:
    summarize_failures(prior_attempts) -> list[str]
        Each string is a multi-line block ready to be bullet-pointed
        into a STRICTLY AVOID section.
"""

from __future__ import annotations

from typing import Any


# Rubric-id → generalised guidance the generator should follow on the
# NEXT attempt. Keyed by both video rubric ids (R1..R10) and image
# rubric ids (I1..I8). Lines are written as imperative directives.
RUBRIC_GUIDANCE: dict[str, str] = {
    # ---- video rubrics (R1..R10) ----
    "R1": (
        "Maintain exact character identity in every frame — same colors, "
        "same armor / cloth pattern, same proportions, same face. NO drift, "
        "NO morphing, NO color shifts across frames."
    ),
    "R2": (
        "Every held item (weapon / shield / tool) must remain visibly "
        "GRASPED by a hand or forearm in every frame. Render the gripping "
        "fingers / forearm explicitly in contact with the item — no "
        "floating items, no items 'near' the body without a visible grip."
    ),
    "R3": (
        "All limbs (arms, legs, or species-equivalent) must remain visible "
        "and connected to the torso in every frame. NO disappearing, NO "
        "duplicating, NO impossible joint angles, NO clipping through body."
    ),
    "R4": (
        "The animation MUST clearly depict the action described. Match the "
        "action keywords literally (walk = legs alternate, attack = clear "
        "strike motion, idle = subtle breathing only)."
    ),
    "R5": (
        "If the action loops, the LAST frame must visually match the FIRST "
        "frame so the loop is seamless. For one-shot actions (death, intro) "
        "this rubric is N/A."
    ),
    "R6": (
        "Character's lowest body part (feet / paws / base) must touch the "
        "bottom canvas edge in every frame. NO drifting upward, NO "
        "floating, NO standing in mid-air. Jump apex is the only "
        "exception."
    ),
    "R7": (
        "All extremities — body parts, weapon, accessories, hair, tail, "
        "cape — must stay strictly INSIDE all four canvas edges in every "
        "frame. **Reduce swing amplitude or angle the motion** so blade "
        "tips / tool ends never extend beyond the canvas. If prior "
        "attempts crossed a specific edge, deliberately swing AWAY from "
        "that edge."
    ),
    "R8": (
        "Lock the camera. NO zoom, NO pan, NO dolly, NO tilt, NO "
        "recompose. Framing identical to the first frame across the "
        "entire video."
    ),
    "R9": (
        "Background color stays pure and uniform — NO gradient, NO "
        "darkening, NO shadow on background, NO color shift."
    ),
    "R10": (
        "ALL visual effects must be FULLY OPAQUE, hard-edged, solid "
        "shapes. NO semi-transparent particles, NO fade-in / fade-out, "
        "NO bloom or halo glow, NO hazy / wispy / misty effects. If "
        "achieving this is impossible for the requested effect, OMIT "
        "the effect entirely rather than render it semi-transparent."
    ),
    # ---- image (single-frame) rubrics (I1..I8) ----
    "I1": (
        "Render the character matching the description literally — correct "
        "species, gender, era, gear. No substitutions."
    ),
    "I2": (
        "Every prop in the image MUST have a clearly visible holding hand "
        "or forearm directly on it. Show fingers / knuckles / forearm "
        "actually gripping the prop. Props near the body without a "
        "visible grip are FORBIDDEN — either render the gripping hand or "
        "remove the prop."
    ),
    "I3": (
        "Render every expected limb visibly, anatomically connected to "
        "the torso, with realistic joint angles. NO missing, duplicate, "
        "or impossibly bent limbs."
    ),
    "I4": (
        "Place the character so the lowest body part (feet / base) lands "
        "in the LOWER PORTION of the canvas — within the bottom 30% of "
        "the canvas height. A small margin of empty space below the feet "
        "is acceptable; the post-matting alignment step pulls feet down "
        "to the canvas bottom. What is FORBIDDEN is vertical centering — "
        "do NOT compose the character in the middle of the canvas with "
        "large empty space below."
    ),
    "I5": (
        "The entire character — body, weapon, accessories, hair, tail — "
        "must fit fully inside the canvas. No clipping at any edge."
    ),
    "I6": (
        "Background must be pure uniform solid color (the requested bg "
        "hex, e.g. #FF00FF or #00F000). NO gradient, NO noise, NO "
        "decoration, NO secondary objects."
    ),
    "I7": (
        "Render exactly ONE character. NO duplicates, NO thumbnails, NO "
        "border, NO UI, NO caption, NO text, NO secondary figures."
    ),
    "I8": (
        "Match the requested view angle literally (side / 3-quarter / "
        "front). Do not rotate the character or change the camera angle."
    ),
}


def _shorten(s: str, n: int = 200) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize_failures(
    prior_attempts: list[dict[str, Any]],
) -> list[str]:
    """Group failures across attempts by rubric ID and synthesise one
    multi-line block per rubric.

    Input: list of attempt-record dicts, each with keys
        ``attempt: int``, ``failures: list[{id, name, evidence}]``.

    Output: a list of strings; each string is a multi-line block ready
    to be bullet-pointed into a STRICTLY AVOID section.
    """
    by_rubric: dict[str, dict[str, Any]] = {}
    for record in prior_attempts:
        attempt_n = record.get("attempt")
        for f in record.get("failures") or []:
            rid = f.get("id") or "?"
            slot = by_rubric.setdefault(
                rid,
                {"name": f.get("name") or "(unnamed)", "evidences": []},
            )
            ev = (f.get("evidence") or "").strip()
            if not ev:
                continue
            # Dedup near-identical evidence within the same rubric — only
            # keep distinct sentence prefixes.
            if any(_shorten(ev, 80) == _shorten(prev[1], 80)
                   for prev in slot["evidences"]):
                continue
            slot["evidences"].append((attempt_n, ev))

    blocks: list[str] = []
    for rid, data in by_rubric.items():
        n_obs = len(data["evidences"])
        ev_lines = "\n".join(
            f"    • attempt {att}: {_shorten(ev)}"
            for att, ev in data["evidences"]
        )
        guidance = RUBRIC_GUIDANCE.get(
            rid, "Avoid repeating this category of failure."
        )
        block = (
            f"[{rid} {data['name']}] OBSERVED IN {n_obs} PRIOR "
            f"ATTEMPT(S):\n{ev_lines}\n"
            f"  → REQUIRED FIX: {guidance}"
        )
        blocks.append(block)
    return blocks


def compose_strictly_avoid_addendum(
    prior_attempts: list[dict[str, Any]],
    *,
    header: str = (
        "ADDITIONAL STRICTLY AVOID (synthesised from prior validation "
        "failures — these issues recurred in earlier attempts and MUST "
        "NOT recur in this one):"
    ),
) -> str:
    """Return the full block to append to the next attempt's prompt,
    or an empty string if there are no prior failures."""
    blocks = summarize_failures(prior_attempts)
    if not blocks:
        return ""
    body = "\n\n".join(blocks)
    return f"{header}\n\n{body}"


__all__ = [
    "RUBRIC_GUIDANCE",
    "compose_strictly_avoid_addendum",
    "summarize_failures",
]
