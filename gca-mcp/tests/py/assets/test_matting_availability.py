from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from gca_mcp.adapters import matting_availability
from gca_mcp.config import Config
from gca_mcp.errors import ToolError


@pytest.fixture(autouse=True)
def _reset_availability():
    matting_availability.reset_rmbg_availability()
    yield
    matting_availability.reset_rmbg_availability()


def test_one_probe_is_shared_by_concurrent_callers():
    config = Config.from_env()
    endpoint = config.rmbg2.endpoint
    owner = matting_availability.acquire_rmbg_route(config, endpoint)
    assert owner.action == "probe"

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(
            matting_availability.acquire_rmbg_route,
            config,
            endpoint,
        )
        assert not waiting.done()
        matting_availability.remember_rmbg_success(owner)
        follower = waiting.result(timeout=2)

    assert follower.action == "rmbg2"
    assert follower.cached is True


def test_transient_probe_failure_stays_on_color_key():
    config = Config.from_env()
    endpoint = config.rmbg2.endpoint
    owner = matting_availability.acquire_rmbg_route(config, endpoint)
    matting_availability.remember_rmbg_error(
        owner,
        ToolError("RMBG2_FAILED", "RMBG2 service returned HTTP 502."),
    )

    first = matting_availability.acquire_rmbg_route(config, endpoint)
    second = matting_availability.acquire_rmbg_route(config, endpoint)

    assert first.action == second.action == "color_key"
    assert first.cached is second.cached is True
    assert first.as_error().code == "RMBG2_FAILED"


def test_missing_dependency_probe_failure_routes_to_color_key():
    config = Config.from_env()
    endpoint = config.rmbg2.endpoint
    owner = matting_availability.acquire_rmbg_route(config, endpoint)
    matting_availability.remember_rmbg_error(
        owner,
        ToolError("MISSING_DEPENDENCY", "Local matting needs torchvision."),
    )

    first = matting_availability.acquire_rmbg_route(config, endpoint)
    second = matting_availability.acquire_rmbg_route(config, endpoint)

    assert first.action == second.action == "color_key"
    assert first.cached is second.cached is True
    assert first.as_error().code == "MISSING_DEPENDENCY"


def test_bad_response_probe_failure_routes_to_color_key():
    config = Config.from_env()
    endpoint = config.rmbg2.endpoint
    owner = matting_availability.acquire_rmbg_route(config, endpoint)
    matting_availability.remember_rmbg_error(
        owner,
        ToolError("BAD_RESPONSE", "RMBG2 returned non-JSON body."),
    )

    first = matting_availability.acquire_rmbg_route(config, endpoint)
    second = matting_availability.acquire_rmbg_route(config, endpoint)

    assert first.action == second.action == "color_key"
    assert first.cached is second.cached is True
    assert first.as_error().code == "BAD_RESPONSE"


def test_non_transient_probe_failure_is_cached_fail_closed():
    config = Config.from_env()
    endpoint = config.rmbg2.endpoint
    owner = matting_availability.acquire_rmbg_route(config, endpoint)
    matting_availability.remember_rmbg_error(
        owner,
        ToolError("AUTH_FAILED", "RMBG2 rejected HTTP 401."),
    )

    cached = matting_availability.acquire_rmbg_route(config, endpoint)

    assert cached.action == "error"
    assert cached.as_error().code == "AUTH_FAILED"


def test_concurrent_transient_error_cannot_downgrade_blocked_state():
    config = Config.from_env()
    endpoint = config.rmbg2.endpoint
    owner = matting_availability.acquire_rmbg_route(config, endpoint)
    matting_availability.remember_rmbg_success(owner)
    auth_request = matting_availability.acquire_rmbg_route(config, endpoint)
    transient_request = matting_availability.acquire_rmbg_route(config, endpoint)

    matting_availability.remember_rmbg_error(
        auth_request,
        ToolError("AUTH_FAILED", "RMBG2 rejected HTTP 401."),
    )
    matting_availability.remember_rmbg_error(
        transient_request,
        ToolError("RMBG2_FAILED", "RMBG2 returned HTTP 502."),
    )

    cached = matting_availability.acquire_rmbg_route(config, endpoint)
    assert cached.action == "error"
    assert cached.as_error().code == "AUTH_FAILED"


def test_late_success_cannot_revive_sticky_fallback():
    config = Config.from_env()
    endpoint = config.rmbg2.endpoint
    owner = matting_availability.acquire_rmbg_route(config, endpoint)
    matting_availability.remember_rmbg_success(owner)
    failed = matting_availability.acquire_rmbg_route(config, endpoint)
    late_success = matting_availability.acquire_rmbg_route(config, endpoint)

    matting_availability.remember_rmbg_error(
        failed,
        ToolError("RMBG2_FAILED", "RMBG2 returned HTTP 502."),
    )
    matting_availability.remember_rmbg_success(late_success)

    assert (
        matting_availability.acquire_rmbg_route(config, endpoint).action
        == "color_key"
    )


def test_stale_reservation_cannot_complete_or_cancel_post_reset_probe():
    config = Config.from_env()
    endpoint = config.rmbg2.endpoint
    stale = matting_availability.acquire_rmbg_route(config, endpoint)
    matting_availability.reset_rmbg_availability()
    current = matting_availability.acquire_rmbg_route(config, endpoint)

    matting_availability.cancel_rmbg_probe(stale)
    matting_availability.remember_rmbg_success(stale)
    matting_availability.remember_rmbg_error(
        stale,
        ToolError("AUTH_FAILED", "stale HTTP 401"),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(
            matting_availability.acquire_rmbg_route,
            config,
            endpoint,
        )
        assert not waiting.done()
        matting_availability.remember_rmbg_success(current)
        assert waiting.result(timeout=2).action == "rmbg2"


def test_reset_allows_one_new_probe():
    config = Config.from_env()
    endpoint = config.rmbg2.endpoint
    owner = matting_availability.acquire_rmbg_route(config, endpoint)
    matting_availability.remember_rmbg_success(owner)

    matting_availability.reset_rmbg_availability()

    assert (
        matting_availability.acquire_rmbg_route(config, endpoint).action
        == "probe"
    )
