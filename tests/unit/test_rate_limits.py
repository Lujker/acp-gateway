"""Admission windows use monotonic time and never charge rejected requests."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from acp_gateway.config import RateLimitSettings, Settings
from acp_gateway.core import RateLimited, rate_limits


def test_window_boundary_retry_and_channel_isolation(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(rate_limits, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    limiter = rate_limits.WorkRateLimiter(RateLimitSettings(work_requests=2, window_seconds=10))
    limiter.check("cli")
    clock[0] = 0.25
    limiter.check("cli")
    limiter.check("mcp")
    for _ in range(100):
        with pytest.raises(RateLimited) as exc:
            limiter.check("cli")
        assert exc.value.retry_after == 10
    clock[0] = 9.1
    with pytest.raises(RateLimited) as exc:
        limiter.check("cli")
    assert exc.value.retry_after == 1
    clock[0] = 10
    limiter.check("cli")
    with pytest.raises(RateLimited):
        limiter.check("cli")
    clock[0] = 10.25
    limiter.check("cli")


@pytest.mark.parametrize(
    "values",
    [
        {"work_requests": 0},
        {"work_requests": 10001},
        {"window_seconds": 0},
        {"window_seconds": 3601},
    ],
)
def test_configuration_bounds(values):
    with pytest.raises(ValidationError):
        RateLimitSettings(**values)


def test_config_environment_and_limiter_copies_settings(monkeypatch):
    monkeypatch.setenv("ACPGW_RATE_LIMITS__WORK_REQUESTS", "3")
    settings = Settings().rate_limits
    limiter = rate_limits.WorkRateLimiter(settings)
    settings.work_requests = 100
    for _ in range(3):
        limiter.check("cli")
    with pytest.raises(RateLimited):
        limiter.check("cli")
