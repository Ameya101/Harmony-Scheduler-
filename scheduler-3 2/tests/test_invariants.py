"""
Invariant tests.

These tests verify all hard constraints on any schedule the solver returns.
Written against the canonical model — valid across any client adapter.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import copy
import pytest

from app.adapters.client_a import parse as parse_client_a
from app.core import InfeasibilityError, solve
from app.core.model import Assignment, ScheduleRequest, ScheduleResult
from tests.fixtures import SAMPLE_INPUT


def _solve_sample(overrides: dict | None = None):
    raw = copy.deepcopy(SAMPLE_INPUT)
    if overrides:
        raw.update(overrides)
    req    = parse_client_a(raw)
    result = solve(req)
    return result, req


# ── Assertion helpers (reusable in tests and validate.py) ────────────────────

def assert_no_overlap(assignments):
    by_res = {}
    for a in assignments:
        by_res.setdefault(a.resource_id, []).append(a)
    for res_id, ops in by_res.items():
        for i, cur in enumerate(sorted(ops, key=lambda x: x.start)):
            for nxt in sorted(ops, key=lambda x: x.start)[i+1:]:
                assert cur.end <= nxt.start, (
                    f"Overlap on {res_id}: {cur.product_id}[{cur.step_index}] ends {cur.end} "
                    f"but {nxt.product_id}[{nxt.step_index}] starts {nxt.start}"
                )


def assert_precedence(assignments, request: ScheduleRequest):
    by_prod = {}
    for a in assignments:
        by_prod.setdefault(a.product_id, []).append(a)
    for prod in request.products:
        ops = sorted(by_prod.get(prod.id, []), key=lambda x: x.step_index)
        for i in range(len(ops) - 1):
            assert ops[i].end <= ops[i+1].start, (
                f"Precedence: {prod.id} step {ops[i].step_index} ends {ops[i].end} "
                f"but step {ops[i+1].step_index} starts {ops[i+1].start}"
            )


def assert_calendar_compliance(assignments, request: ScheduleRequest):
    res_by_id = {r.id: r for r in request.resources}
    for a in assignments:
        res  = res_by_id[a.resource_id]
        fits = any(win.contains(a.start, a.end) for win in res.calendar)
        assert fits, (
            f"Calendar: {a.product_id}[{a.step_index}] on {a.resource_id} "
            f"[{a.start}, {a.end}] fits no calendar window"
        )


def assert_horizon_bounds(assignments, request: ScheduleRequest):
    for a in assignments:
        assert a.start >= request.horizon_start, f"{a.product_id} starts before horizon"
        assert a.end   <= request.horizon_end,   f"{a.product_id} ends after horizon"


def assert_changeovers(result: ScheduleResult, request: ScheduleRequest):
    by_res = {}
    for a in result.assignments:
        by_res.setdefault(a.resource_id, []).append(a)
    prod_family = {p.id: p.family for p in request.products}
    for res_id, ops in by_res.items():
        for i, cur in enumerate(sorted(ops, key=lambda x: x.start)):
            for nxt in sorted(ops, key=lambda x: x.start)[i+1:]:
                fam_a    = prod_family[cur.product_id]
                fam_b    = prod_family[nxt.product_id]
                required = request.changeover_matrix.lookup(fam_a, fam_b)
                if required > 0:
                    gap = (nxt.start - cur.end).total_seconds() / 60
                    assert gap >= required - 0.01, (
                        f"Changeover on {res_id}: {cur.product_id}({fam_a}) → "
                        f"{nxt.product_id}({fam_b}) needs {required} min, got {gap:.1f}"
                    )


def assert_capability_eligibility(assignments, request: ScheduleRequest):
    res_caps = {r.id: r.capabilities for r in request.resources}
    for a in assignments:
        assert a.capability in res_caps[a.resource_id], (
            f"{a.resource_id} lacks '{a.capability}' for {a.product_id}[{a.step_index}]"
        )


def full_invariant_check(result: ScheduleResult, request: ScheduleRequest):
    assert_no_overlap(result.assignments)
    assert_precedence(result.assignments, request)
    assert_calendar_compliance(result.assignments, request)
    assert_horizon_bounds(result.assignments, request)
    assert_changeovers(result, request)
    assert_capability_eligibility(result.assignments, request)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_no_overlap():
    result, req = _solve_sample()
    assert_no_overlap(result.assignments)


def test_precedence():
    result, req = _solve_sample()
    assert_precedence(result.assignments, req)


def test_calendar_compliance():
    result, req = _solve_sample()
    assert_calendar_compliance(result.assignments, req)


def test_horizon_bounds():
    result, req = _solve_sample()
    assert_horizon_bounds(result.assignments, req)


def test_changeover_gaps():
    result, req = _solve_sample()
    assert_changeovers(result, req)


def test_capability_eligibility():
    result, req = _solve_sample()
    assert_capability_eligibility(result.assignments, req)


def test_all_products_scheduled():
    result, req = _solve_sample()
    scheduled = {a.product_id for a in result.assignments}
    for prod in req.products:
        assert prod.id in scheduled, f"{prod.id} not scheduled"


def test_all_steps_scheduled():
    """Every route step must appear exactly once in the assignments."""
    result, req = _solve_sample()
    for prod in req.products:
        for s_idx, _ in enumerate(prod.route):
            hits = [a for a in result.assignments
                    if a.product_id == prod.id and a.step_index == s_idx + 1]
            assert len(hits) == 1, (
                f"{prod.id} step {s_idx+1}: expected 1 assignment, got {len(hits)}"
            )


def test_full_invariants():
    result, req = _solve_sample()
    full_invariant_check(result, req)


def test_determinism():
    """Same input must produce identical output every time.
    
    Uses num_workers=8 + random_seed=42 — faster AND deterministic.
    """
    result1, _ = _solve_sample()
    result2, _ = _solve_sample()
    s1 = [(a.product_id, a.step_index, a.start, a.resource_id) for a in result1.assignments]
    s2 = [(a.product_id, a.step_index, a.start, a.resource_id) for a in result2.assignments]
    assert s1 == s2, "Solver produced different schedules for identical input"


def test_solver_status_present():
    """solver_status must be 'optimal' or 'feasible_not_optimal'."""
    result, _ = _solve_sample()
    assert result.solver_status in ("optimal", "feasible_not_optimal")


def test_split_calendar_window_compliance():
    """Fill-1 has a break 12:00–12:30; ops must not straddle it."""
    result, req = _solve_sample()
    fill1_ops = [a for a in result.assignments if a.resource_id == "Fill-1"]
    for a in fill1_ops:
        # Must not overlap the break window
        from datetime import datetime
        break_start = datetime(2025, 11, 3, 12, 0)
        break_end   = datetime(2025, 11, 3, 12, 30)
        overlaps_break = a.start < break_end and a.end > break_start
        assert not overlaps_break, (
            f"Fill-1 op {a.product_id}[{a.step_index}] "
            f"[{a.start.time()}-{a.end.time()}] straddles the lunch break"
        )


def test_no_dead_prod_index_function():
    """
    FIX #2: _prod_index() was a dead O(n) scan function.
    Verify it no longer exists in scheduler.py.
    """
    import inspect
    from app.core import scheduler as sched_mod
    assert not hasattr(sched_mod, "_prod_index"), (
        "_prod_index still exists — it was dead code and should be removed"
    )


def test_warm_start_hint_does_not_break_solution():
    """
    FIX #7: Greedy warm start hints must not break correctness.
    The solver should still return a valid optimal/feasible solution.
    """
    result, req = _solve_sample()
    # If warm start broke something, invariants would fail
    full_invariant_check(result, req)
    assert len(result.assignments) == 11
