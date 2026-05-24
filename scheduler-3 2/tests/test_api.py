"""
HTTP integration tests for POST /schedule.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import copy
import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.fixtures import SAMPLE_INPUT

client = TestClient(app)


def test_returns_200():
    assert client.post("/schedule", json=SAMPLE_INPUT).status_code == 200


def test_response_shape():
    body = client.post("/schedule", json=SAMPLE_INPUT).json()
    assert "assignments" in body
    assert "kpis" in body
    assert "solver_status" in body
    for field in ("tardiness_minutes", "changeover_count", "changeover_minutes",
                  "makespan_minutes", "utilization_pct"):
        assert field in body["kpis"], f"KPI field '{field}' missing"
    for a in body["assignments"]:
        for field in ("product", "step_index", "capability", "resource", "start", "end"):
            assert field in a, f"Assignment field '{field}' missing"


def test_assignment_count():
    """P-100(3) + P-101(3) + P-102(2) + P-103(3) = 11 steps."""
    body = client.post("/schedule", json=SAMPLE_INPUT).json()
    assert len(body["assignments"]) == 11


def test_utilization_all_resources_present():
    body  = client.post("/schedule", json=SAMPLE_INPUT).json()
    expected = {"Fill-1", "Fill-2", "Label-1", "Pack-1"}
    assert set(body["kpis"]["utilization_pct"].keys()) == expected


def test_solver_status_value():
    body = client.post("/schedule", json=SAMPLE_INPUT).json()
    assert body["solver_status"] in ("optimal", "feasible_not_optimal")


def test_validation_error_inverted_horizon():
    bad = copy.deepcopy(SAMPLE_INPUT)
    bad["horizon"] = {"start": "2025-11-03T16:00:00", "end": "2025-11-03T08:00:00"}
    resp = client.post("/schedule", json=bad)
    assert resp.status_code == 422


def test_validation_error_missing_products():
    bad = {k: v for k, v in SAMPLE_INPUT.items() if k != "products"}
    assert client.post("/schedule", json=bad).status_code == 422


def test_min_makespan_objective():
    """min_makespan must return a valid schedule (extensibility check)."""
    raw = copy.deepcopy(SAMPLE_INPUT)
    raw["settings"]["objective_mode"] = "min_makespan"
    resp = client.post("/schedule", json=raw)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["assignments"]) == 11


def test_unknown_objective_returns_501():
    raw = copy.deepcopy(SAMPLE_INPUT)
    raw["settings"]["objective_mode"] = "min_unicorns"
    resp = client.post("/schedule", json=raw)
    assert resp.status_code == 422   # caught at validation layer


def test_feasible_not_optimal_warning_present(monkeypatch):
    """If solver_status is feasible_not_optimal, a warning must appear in the response."""
    from app.core import scheduler as sched_mod
    from app.core.model import ScheduleResult

    original_solve = sched_mod.solve

    def mock_solve(req):
        result = original_solve(req)
        result.solver_status = "feasible_not_optimal"
        return result

    monkeypatch.setattr("app.api.routes.solve", mock_solve)
    body = client.post("/schedule", json=SAMPLE_INPUT).json()
    assert "warning" in body
    assert "time limit" in body["warning"].lower()


def test_health_check():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
