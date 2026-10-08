"""Process-local RMBG availability memory for ``auto`` matting.

The first auto-matting caller for one endpoint owns the probe. Concurrent
callers wait for that result, then reuse it for the lifetime of the MCP
process. Explicit ``rmbg2`` calls intentionally bypass this cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Condition
from typing import Literal

from ..errors import ToolError, is_transient_rmbg_error

RouteAction = Literal["probe", "rmbg2", "color_key", "error"]
_StateName = Literal["probing", "available", "unavailable", "blocked"]
_CacheKey = str


@dataclass(frozen=True)
class RmbgRoute:
    """Resolved auto-matting route for one endpoint."""

    key: _CacheKey
    generation: int
    action: RouteAction
    cached: bool
    code: str | None = None
    message: str | None = None
    hint: str | None = None

    def as_error(self) -> ToolError:
        return ToolError(
            self.code or "RMBG2_FAILED",
            self.message or "Cached RMBG probe failed.",
            hint=self.hint,
        )


@dataclass
class _Availability:
    status: _StateName
    generation: int
    code: str | None = None
    message: str | None = None
    hint: str | None = None


_CONDITION = Condition()
_AVAILABILITY: dict[_CacheKey, _Availability] = {}
_GENERATION = 0


def _next_generation() -> int:
    global _GENERATION
    _GENERATION += 1
    return _GENERATION


def _cache_key(config: object, endpoint: str) -> _CacheKey:
    return str(endpoint).rstrip("/")


def acquire_rmbg_route(config: object, endpoint: str) -> RmbgRoute:
    """Return a cached route or reserve the process's sole probe."""

    key = _cache_key(config, endpoint)
    with _CONDITION:
        while True:
            state = _AVAILABILITY.get(key)
            if state is None:
                generation = _next_generation()
                _AVAILABILITY[key] = _Availability(
                    status="probing",
                    generation=generation,
                )
                return RmbgRoute(
                    key=key,
                    generation=generation,
                    action="probe",
                    cached=False,
                )
            if state.status == "probing":
                _CONDITION.wait()
                continue
            if state.status == "available":
                return RmbgRoute(
                    key=key,
                    generation=state.generation,
                    action="rmbg2",
                    cached=True,
                )
            action: RouteAction = (
                "color_key" if state.status == "unavailable" else "error"
            )
            return RmbgRoute(
                key=key,
                generation=state.generation,
                action=action,
                cached=True,
                code=state.code,
                message=state.message,
                hint=state.hint,
            )


def remember_rmbg_success(route: RmbgRoute) -> None:
    """Complete a probe successfully without reviving a sticky failure."""

    with _CONDITION:
        state = _AVAILABILITY.get(route.key)
        if (
            state is not None
            and state.generation == route.generation
            and state.status in {"probing", "available"}
        ):
            _AVAILABILITY[route.key] = _Availability(
                status="available",
                generation=route.generation,
            )
        _CONDITION.notify_all()


def remember_rmbg_error(route: RmbgRoute, error: ToolError) -> None:
    """Remember fallback-eligible outages and fail-closed provider errors."""

    status: _StateName = (
        "unavailable" if is_transient_rmbg_error(error) else "blocked"
    )
    with _CONDITION:
        current = _AVAILABILITY.get(route.key)
        if current is None or current.generation != route.generation:
            _CONDITION.notify_all()
            return
        # Fail-closed is monotonic: an auth/config/provider error discovered by
        # one in-flight request must not be downgraded to color-key fallback by
        # a later-finishing transient 5xx from a sibling request.
        if current is not None and current.status == "blocked":
            _CONDITION.notify_all()
            return
        if current.status == "unavailable" and status == "unavailable":
            _CONDITION.notify_all()
            return
        _AVAILABILITY[route.key] = _Availability(
            status=status,
            generation=route.generation,
            code=str(error.code),
            message=error.message,
            hint=error.hint,
        )
        _CONDITION.notify_all()


def cancel_rmbg_probe(route: RmbgRoute) -> None:
    """Release a probe reservation after an unexpected local exception."""

    if route.action != "probe":
        return
    with _CONDITION:
        state = _AVAILABILITY.get(route.key)
        if (
            state is not None
            and state.generation == route.generation
            and state.status == "probing"
        ):
            _AVAILABILITY.pop(route.key, None)
        _CONDITION.notify_all()


def reset_rmbg_availability() -> None:
    """Clear process memory (tests and explicit runtime reconfiguration)."""

    with _CONDITION:
        _next_generation()
        _AVAILABILITY.clear()
        _CONDITION.notify_all()


__all__ = [
    "RmbgRoute",
    "acquire_rmbg_route",
    "cancel_rmbg_probe",
    "remember_rmbg_error",
    "remember_rmbg_success",
    "reset_rmbg_availability",
]
