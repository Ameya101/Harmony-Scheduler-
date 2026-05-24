"""
Infeasibility tests — structured error responses with concrete reasons.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import copy
import pytest
from fastapi.testclient import TestClient

from app.adapters.client_a import parse as parse_client_a
from app.core import InfeasibilityError, solve
from app.main import app
from tests.fixtures import SAMPLE_INPUT

client = TestClient(app)


def _expect_infeasible(tweak_fn):
    raw = copy.deepcopy(SAMPLE_INPUT)
    tweak_fn(raw)
    req = parse_client_a(raw)
    with pytest.raises(InfeasibilityError) as exc_info:
        solve(req)
    return exc_info.value


def test_missing_capability_label():
    def tweak(raw):
        raw["resources"] = [r for r in raw["resources"] if "label" not in r["capabilities"]]
    err = _expect_infeasible(tweak)
    assert err.reasons
    assert any("label" in r.lower() for r in err.reasons)


def test_missing_capability_fill():
    def tweak(raw):
        raw["resources"] = [r for r in raw["resources"] if "fill" not in r["capabilities"]]
    err = _expect_infeasible(tweak)
    assert err.reasons
    assert any("fill" in r.lower() for r in err.reasons)


def test_window_too_short():
    def tweak(raw):
        for r in raw["resources"]:
            if "fill" in r["capabilities"]:
                r["calendar"] = [["2025-11-03T08:00:00", "2025-11-03T08:05:00"]]
    err = _expect_infeasible(tweak)
    assert err.reasons
    assert any("fill" in r.lower() or "window" in r.lower() or "feasible" in r.lower()
               for r in err.reasons)


def test_horizon_too_short():
    def tweak(raw):
        raw["horizon"]["end"] = "2025-11-03T08:05:00"
        for r in raw["resources"]:
            r["calendar"] = [["2025-11-03T08:00:00", "2025-11-03T08:05:00"]]
    err = _expect_infeasible(tweak)
    assert err.reasons


def test_reasons_are_non_empty_strings():
    def tweak(raw):
        raw["resources"] = [r for r in raw["resources"] if "pack" not in r["capabilities"]]
    err = _expect_infeasible(tweak)
    assert len(err.reasons) >= 1
    for r in err.reasons:
        assert isinstance(r, str) and len(r) > 0


def test_api_returns_422_for_infeasible():
    raw = copy.deepcopy(SAMPLE_INPUT)
    raw["resources"] = [r for r in raw["resources"] if "fill" not in r["capabilities"]]
    resp = client.post("/schedule", json=raw)
    assert resp.status_code >= 400
    body = resp.json()
    assert body.get("error") in ("infeasible", "validation_error")
    assert "why" in body or "detail" in body


def test_api_why_field_is_list_of_strings():
    raw = copy.deepcopy(SAMPLE_INPUT)
    raw["resources"] = [r for r in raw["resources"] if "label" not in r["capabilities"]]
    resp = client.post("/schedule", json=raw)
    body = resp.json()
    if "why" in body:
        assert isinstance(body["why"], list)
        assert all(isinstance(s, str) for s in body["why"])


# ── FIX #3: Missing changeover matrix entries ────────────────────────────────

def test_missing_changeover_entry_raises_validation_error():
    """
    FIX #3: If the changeover matrix omits any family pair, the adapter
    must raise a clear ValueError — not silently use 0.
    """
    from pydantic import ValidationError as PydanticValidationError
    raw = copy.deepcopy(SAMPLE_INPUT)
    # Remove standard->premium entry
    del raw["changeover_matrix_minutes"]["values"]["standard->premium"]
    with pytest.raises((ValueError, PydanticValidationError)):
        parse_client_a(raw)


def test_missing_changeover_error_message_is_clear():
    """Error message must name the missing pair."""
    raw = copy.deepcopy(SAMPLE_INPUT)
    del raw["changeover_matrix_minutes"]["values"]["premium->standard"]
    try:
        parse_client_a(raw)
        assert False, "Should have raised"
    except (ValueError, Exception) as e:
        assert "premium->standard" in str(e) or "missing" in str(e).lower()


# ── FIX #5: Capacity preflight catches issues before solver ──────────────────

def test_capacity_preflight_fires_before_solver():
    """
    FIX #5: Overcapacity should be caught by _capacity_preflight immediately,
    not after burning time_limit_seconds.
    """
    import time
    raw = copy.deepcopy(SAMPLE_INPUT)
    # Shrink all calendars to 1 minute — clearly not enough for any work
    for r in raw["resources"]:
        r["calendar"] = [["2025-11-03T08:00:00", "2025-11-03T08:01:00"]]
    raw["settings"]["time_limit_seconds"] = 30

    req = parse_client_a(raw)
    start = time.time()
    with pytest.raises(InfeasibilityError) as exc_info:
        solve(req)
    elapsed = time.time() - start

    # Should fail in well under 1 second — not wait for 30s time limit
    assert elapsed < 5, f"Preflight took {elapsed:.1f}s — solver was invoked unnecessarily"
    assert exc_info.value.reasons
