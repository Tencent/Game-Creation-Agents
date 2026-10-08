"""Canonical error codes for the GCA MCP tool surface.

Mirrors the `video-creation-mcp` design — a stable enum so consumers
(agents, hooks, tests) can reason about failures without parsing free-
form messages. Dynamic per-provider codes follow documented prefixes.
"""

from __future__ import annotations

import re
from typing import Literal

ErrorCode = Literal[
    # ---- workspace lifecycle ----
    "WORKSPACE_ALREADY_OPEN",
    "WORKSPACE_CLOSED",
    "WORKSPACE_INIT_ERROR",
    "WORKSPACE_MISMATCH",
    "WORKSPACE_NOT_CLEAN",
    "WORKSPACE_NOT_RESUMABLE",
    "WORKSPACE_REPLACED",
    "NO_WORKSPACE_OPEN",
    "INVALID_WORKSPACE",
    "INVALID_PATH",
    "PATH_ESCAPE",
    "PATH_NOT_ALLOWED",
    "NON_CANONICAL_PATH",
    # ---- credentials / config ----
    "MISSING_CREDENTIALS",
    "INVALID_CONFIG",
    "INVALID_ARGUMENT",
    "INVALID_GENRE",
    "INVALID_INTERACTION_PROFILE",
    "INVALID_LEVEL_MODEL",
    "INVALID_QUALITY_PROFILE",
    # ---- coordination / state transactions ----
    "LOCK_ERROR",
    "LOCK_REENTRY",
    "LOCK_REPLACED",
    "INVALID_STATE_TRANSACTION",
    "STATE_TRANSACTION_MISMATCH",
    "STATE_TRANSACTION_PENDING",
    "INVALID_STATE_TRANSITION",
    "ITERATION_BUDGET_EXHAUSTED",
    # ---- artifacts / outputs ----
    "INPUT_FILE_NOT_FOUND",
    "INPUT_NOT_FOUND",
    "OUTPUT_EXISTS",
    "OUTPUT_NOT_FOUND",
    "NON_CANONICAL_OUTPUT",
    "INVALID_ARTIFACT",
    "MISSING_ARTIFACT",
    "MISSING_METADATA",
    # ---- image generation (gpt-image-2) ----
    "IMAGE_GEN_FAILED",
    "IMAGE_EDIT_FAILED",
    "IMAGE_RATE_LIMITED",
    "IMAGE_INPUT_NOT_FOUND",
    # ---- seedance video generation ----
    "SEEDANCE_FAILED",
    "SEEDANCE_NO_OUTPUT",
    "SEEDANCE_TIMEOUT",
    # ---- video understanding (Doubao) ----
    "VLM_CALL_FAILED",
    "VLM_TIMEOUT",
    "VLM_PARSE_FAILED",
    "FFMPEG_MISSING",
    # ---- post-processing ----
    "POSTPROCESS_FAILED",
    "COLOR_KEY_MATTING_FAILED",
    "INVALID_IMAGE",
    "RMBG2_FAILED",
    # ---- concept grounding (multimodal GDD) ----
    "GROUNDING_FAILED",
    # ---- pipeline / routing contracts ----
    "PIPELINE_IDENTITY_MISSING",
    "MISSING_ROUTING_METADATA",
    "MISSING_RUN_IDENTITY",
    "ROUTING_METADATA_IMMUTABLE",
    "ROUTING_METADATA_MISMATCH",
    "ROUTING_METADATA_MISSING",
    "STALE_PIPELINE_STATE",
    "INVALID_RECEIPT",
    "REPLAYED_RECEIPT",
    "STALE_RECEIPT",
    "INVALID_RELEASE_WAIVER",
    "STALE_RELEASE_WAIVER",
    "INVALID_SCAFFOLD_DECLARATION",
    # ---- verify / gate-check ----
    "GATE_BLOCKED",
    "GAME_DIR_NOT_FOUND",
    "STATE_JSON_MISSING",
    "COUNTER_EVIDENCE_MISMATCH",
    "INVALID_ASSET_INTEGRATION",
    "INVALID_SLOT_VALIDATION",
    "SLOT_ARTIFACT_MISMATCH",
    "SLOT_DESCRIPTION_MISMATCH",
    "STALE_ASSET_INTEGRATION",
    "INVALID_VERIFY_MODE",
    "INVALID_VERIFY_URL",
    "VERIFY_URL_REQUIRED",
    "VERIFY_URL_MISMATCH",
    "VERIFY_MODE_MISMATCH",
    "VERIFY_RUN_ACTIVE",
    "VERIFY_RUN_FINALIZED",
    "VERIFY_RUN_IDENTITY_MISMATCH",
    "VERIFY_RUN_ID_MISMATCH",
    "VERIFY_RUN_ID_REQUIRED",
    "VERIFY_RUN_NOT_ACTIVE",
    "VERIFY_RUN_NOT_STARTED",
    "VERIFY_RUN_STATE_INVALID",
    "RUNTIME_CAPTURE_FAILED",
    "RUNTIME_IDENTITY_MISMATCH",
    "RUNTIME_IDENTITY_MISSING",
    "RUNTIME_TOKEN_RECEIPT_INVALID",
    # ---- video evidence ----
    "FOREIGN_VIDEO",
    "VIDEO_ANALYSIS_INCOMPLETE",
    "VIDEO_RECEIPT_INVALID",
    # ---- transport ----
    "NETWORK_ERROR",
    "AUTH_FAILED",
    "BAD_RESPONSE",
    # ---- fallbacks ----
    "UNKNOWN_TOOL",
    "INTERNAL_ERROR",
]


ERROR_CODES: dict[str, str] = {
    # ---- workspace lifecycle ----
    "WORKSPACE_ALREADY_OPEN": "A workspace is already open at a different path; restart to switch.",
    "WORKSPACE_CLOSED": "The pinned workspace identity is no longer held.",
    "WORKSPACE_INIT_ERROR": "Failed to open or create the workspace at the given path.",
    "WORKSPACE_MISMATCH": "The requested game directory does not match the active workspace.",
    "WORKSPACE_NOT_CLEAN": "Create mode requires a new, clean game workspace.",
    "WORKSPACE_NOT_RESUMABLE": "Resume mode could not find valid pipeline state to resume.",
    "WORKSPACE_REPLACED": "The workspace or its parent changed after initialization.",
    "NO_WORKSPACE_OPEN": "Tool requires an open workspace. Call init_workspace(path=…, mode=…) first.",
    "INVALID_WORKSPACE": "The active workspace lacks the required pinned authority.",
    "INVALID_PATH": "Path argument is missing or not a non-empty string.",
    "PATH_ESCAPE": "A resolved path escapes its required workspace boundary.",
    "PATH_NOT_ALLOWED": "The path is outside the locations allowed for this operation.",
    "NON_CANONICAL_PATH": "The path is not in the required canonical workspace form.",

    # ---- credentials / config ----
    "MISSING_CREDENTIALS": "Required provider API credentials are not set.",
    "INVALID_CONFIG": "Tool configuration is invalid; check env vars or arguments.",
    "INVALID_ARGUMENT": "Tool arguments do not satisfy the declared input schema.",
    "INVALID_GENRE": "The pipeline genre is missing or not a supported genre slug.",
    "INVALID_INTERACTION_PROFILE": "The interaction profile is missing or unsupported.",
    "INVALID_LEVEL_MODEL": "The level model is missing, unsupported, or internally inconsistent.",
    "INVALID_QUALITY_PROFILE": "The quality profile is missing or unsupported.",

    # ---- coordination / state transactions ----
    "LOCK_ERROR": "A secure coordination lock could not be opened, used, or validated.",
    "LOCK_REENTRY": "An exclusive coordination lock was re-entered on the same thread.",
    "LOCK_REPLACED": "A coordination lock or its parent directory was replaced while in use.",
    "INVALID_STATE_TRANSACTION": "The pending pipeline state transaction is malformed or invalid.",
    "STATE_TRANSACTION_MISMATCH": "Pipeline state and resume metadata do not share one transaction identity.",
    "STATE_TRANSACTION_PENDING": "A previous state/resume transaction is incomplete.",
    "INVALID_STATE_TRANSITION": "Pipeline state transition violates counter or audit integrity.",

    # ---- artifacts / outputs ----
    "INPUT_FILE_NOT_FOUND": "A required input file does not exist.",
    "INPUT_NOT_FOUND": "A required workspace-relative input does not exist.",
    "OUTPUT_EXISTS": "The requested output already exists and cannot be overwritten.",
    "OUTPUT_NOT_FOUND": "The expected output was not produced.",
    "NON_CANONICAL_OUTPUT": "The output path is not in the required canonical location.",
    "INVALID_ARTIFACT": "An artifact or manifest entry is malformed or violates its contract.",
    "MISSING_ARTIFACT": "A required generated or runtime artifact does not exist.",
    "MISSING_METADATA": "Required artifact metadata is missing.",

    # ---- image generation (gpt-image-2) ----
    "IMAGE_GEN_FAILED": "Image generation request failed.",
    "IMAGE_EDIT_FAILED": "Image edit request failed.",
    "IMAGE_RATE_LIMITED": "Image generation rate-limited after retries.",
    "IMAGE_INPUT_NOT_FOUND": "Reference image input file does not exist.",

    # ---- seedance video generation ----
    "SEEDANCE_FAILED": "Seedance video generation request failed.",
    "SEEDANCE_NO_OUTPUT": "Seedance returned success but no video_url in response.",
    "SEEDANCE_TIMEOUT": "Seedance video generation did not finish before its timeout.",

    # ---- video understanding (Doubao) ----
    "VLM_CALL_FAILED": "Video / image understanding model call failed.",
    "VLM_TIMEOUT": "Video understanding model call exceeded timeout.",
    "VLM_PARSE_FAILED": "The VLM response could not be parsed into the required structure.",
    "FFMPEG_MISSING": "ffmpeg/ffprobe not available; required for video segmentation.",

    # ---- post-processing ----
    "POSTPROCESS_FAILED": "Post-processing or media conversion failed.",
    "COLOR_KEY_MATTING_FAILED": "Color-key matting did not produce a usable transparent image.",
    "INVALID_IMAGE": "Input image could not be opened or is malformed.",
    "RMBG2_FAILED": "RMBG-2.0 background-removal service returned an error.",

    # ---- concept grounding (multimodal GDD) ----
    "GROUNDING_FAILED": "VLM concept-mockup grounding produced no parseable bounding boxes.",

    # ---- pipeline / routing contracts ----
    "PIPELINE_IDENTITY_MISSING": "The pipeline state lacks the identity required for verification.",
    "MISSING_ROUTING_METADATA": "Required pipeline routing metadata has not been initialized.",
    "MISSING_RUN_IDENTITY": "The operation requires an initialized pipeline run identity.",
    "ROUTING_METADATA_IMMUTABLE": "Routing metadata cannot change within the active pipeline run.",
    "ROUTING_METADATA_MISMATCH": "Pipeline routing fields contradict their canonical design axes.",
    "ROUTING_METADATA_MISSING": "Pipeline state does not contain complete valid routing metadata.",
    "STALE_PIPELINE_STATE": "Build inputs changed after the last pipeline state write.",
    "INVALID_RECEIPT": "A pipeline receipt is missing, malformed, stale, or cross-run.",
    "REPLAYED_RECEIPT": "A pinned receipt or authority file was already consumed or published.",
    "STALE_RECEIPT": "A sealed receipt no longer matches the current files or build.",
    "INVALID_RELEASE_WAIVER": "A release waiver is malformed, expired, or does not match the run.",
    "STALE_RELEASE_WAIVER": "A build change requires a newly approved release waiver.",
    "INVALID_SCAFFOLD_DECLARATION": "The scaffold reproject declaration is malformed or unauthorized.",

    # ---- verify / gate-check ----
    "GATE_BLOCKED": "Pipeline gate failed — see report for blocked items.",
    "GAME_DIR_NOT_FOUND": "Game directory does not exist (expected under playground/).",
    "STATE_JSON_MISSING": ".playtest/state.json not found in the game directory.",
    "COUNTER_EVIDENCE_MISMATCH": "Guarded counter evidence does not match canonical pipeline state.",
    "INVALID_ASSET_INTEGRATION": "The asset integration report is missing, invalid, or not current.",
    "INVALID_SLOT_VALIDATION": "An asset-slot validation receipt is invalid, stale, or forged.",
    "SLOT_ARTIFACT_MISMATCH": "The validated artifact does not match its canonical manifest slot.",
    "SLOT_DESCRIPTION_MISMATCH": "The slot description does not match the canonical manifest or GDD.",
    "STALE_ASSET_INTEGRATION": "The asset integration report predates current build inputs.",
    "INVALID_VERIFY_MODE": "The requested verification mode is not supported.",
    "INVALID_VERIFY_URL": "The verification URL is malformed or not canonical.",
    "VERIFY_URL_REQUIRED": "A non-empty URL is required for the verification epoch.",
    "VERIFY_URL_MISMATCH": "The producer URL does not match the active verification epoch.",
    "VERIFY_MODE_MISMATCH": "The operation mode does not match the active verification run.",
    "VERIFY_RUN_ACTIVE": "Build mutation is blocked while a verification run is active.",
    "VERIFY_RUN_FINALIZED": "The verification run is finalized and cannot accept more evidence.",
    "VERIFY_RUN_IDENTITY_MISMATCH": "The active verification identity does not match pipeline state.",
    "VERIFY_RUN_ID_MISMATCH": "The supplied verification run ID is stale or belongs to another run.",
    "VERIFY_RUN_ID_REQUIRED": "The exact active verification run ID is required.",
    "VERIFY_RUN_NOT_ACTIVE": "The verification run is not active for this operation.",
    "VERIFY_RUN_NOT_STARTED": "Begin a verification run before producing evidence.",
    "VERIFY_RUN_STATE_INVALID": "The persisted verification run state is unreadable or invalid.",
    "RUNTIME_CAPTURE_FAILED": "Runtime asset capture failed.",
    "RUNTIME_IDENTITY_MISMATCH": "Runtime evidence belongs to another URL, workspace, or run.",
    "RUNTIME_IDENTITY_MISSING": "The browser producer did not return runtime identity evidence.",
    "RUNTIME_TOKEN_RECEIPT_INVALID": "The runtime-token receipt is missing, invalid, stale, or cross-run.",

    # ---- video evidence ----
    "FOREIGN_VIDEO": "The selected video is not a current canonical scenario recording.",
    "VIDEO_ANALYSIS_INCOMPLETE": "Video analysis did not complete successfully.",
    "VIDEO_RECEIPT_INVALID": "The video receipt is invalid, stale, altered, or cross-run.",

    # ---- transport ----
    "NETWORK_ERROR": "HTTP transport error reaching the provider API.",
    "AUTH_FAILED": "Provider API rejected the configured credentials.",
    "BAD_RESPONSE": "Provider API returned an unparseable response body.",

    # ---- fallbacks ----
    "UNKNOWN_TOOL": "The requested MCP tool name is not registered.",
    "INTERNAL_ERROR": "Unhandled internal exception — report with the tool name and args.",
}


class ToolError(Exception):
    """Structured error with a stable code, human message, and optional hint.

    The MCP stdio layer catches this and renders a JSON error payload.
    """

    __slots__ = ("code", "message", "hint")

    def __init__(self, code: ErrorCode | str, message: str, hint: str | None = None) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(message)


def is_transient_rmbg_error(error: ToolError) -> bool:
    """Return whether an RMBG failure is safe to route to a keyed fallback.

    A missing remote route (HTTP 404) or malformed provider image is
    fallback-eligible because neither produced usable matting output. Other
    HTTP 4xx responses remain fail-closed so authentication and malformed-input
    failures are not hidden.

    MISSING_DEPENDENCY means the local-rmbg extra (torch/torchvision) is not
    installed; under ``auto`` this is treated as permanent unavailability so
    color-key fallback applies — installing the extra and restarting the server
    will restore the RMBG path.  BAD_RESPONSE means the remote service returned
    garbled or non-JSON output, which is a transient service-side condition.
    """
    if error.code in {"NETWORK_ERROR", "MISSING_DEPENDENCY", "BAD_RESPONSE"}:
        return True
    if error.code != "RMBG2_FAILED":
        return False
    match = re.search(r"\bHTTP\s+(\d{3})\b", error.message, flags=re.IGNORECASE)
    if not match:
        return "malformed image response" in error.message.lower()
    status = int(match.group(1))
    return status == 404 or 500 <= status < 600


def is_transient_seedance_error(error: ToolError) -> bool:
    """Return whether Seedance produced no usable video for a transient reason.

    Authentication, configuration, policy, and ordinary HTTP 4xx failures are
    terminal. Network failures, timeouts, malformed/empty responses, rate
    limits, and HTTP 5xx responses may be retried because no usable video was
    returned to the caller.
    """
    if error.code in {
        "NETWORK_ERROR",
        "SEEDANCE_TIMEOUT",
        "SEEDANCE_NO_OUTPUT",
        "BAD_RESPONSE",
    }:
        return True
    if error.code != "SEEDANCE_FAILED":
        return False
    match = re.search(r"\bHTTP\s+(\d{3})\b", error.message, flags=re.IGNORECASE)
    if match:
        status = int(match.group(1))
        return status in {408, 409, 425, 429} or 500 <= status < 600
    message = error.message.lower()
    terminal_markers = (
        "auth",
        "content policy",
        "credential",
        "forbidden",
        "invalid argument",
        "invalid request",
        "permission",
        "safety",
        "unsupported",
    )
    if any(marker in message for marker in terminal_markers):
        return False
    transient_markers = (
        "connection",
        "provider",
        "network",
        "rate limit",
        "temporar",
        "timed out",
        "timeout",
        "too many requests",
        "unavailable",
        "upstream request failed",
    )
    return any(marker in message for marker in transient_markers)


def is_known_code(code: str) -> bool:
    return code in ERROR_CODES


__all__ = [
    "ERROR_CODES",
    "ErrorCode",
    "ToolError",
    "is_known_code",
    "is_transient_rmbg_error",
    "is_transient_seedance_error",
]
