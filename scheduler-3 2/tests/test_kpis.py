"""
KPI tests — verify correctness and reproducibility.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import copy
import pytest

from app.adapters.client_a import parse as parse_client_a
from app.core import solve, compute_kpis
from tests.fixtures import SAMPLE_INPUT


def _run(raw=None):
    raw = copy.deepcopy(raw or SAMPLE_INPUT)
    req    = parse_client_a(raw)
    result = solve(req)
    kpis   = compute_kpis(result, req)
    return result, kpis, req


def test_tardiness_non_negative():
    _, kpis, _ = _run()
    assert kpis.tardiness_minutes >= 0


def test_tardiness_reproducible():
    """Recomputing tardiness from assignments must match the reported value."""
    result, kpis, req = _run()
    prod_completion = {}
    for a in result.assignments:
        prev = prod_completion.get(a.product_id)
        if prev is None or a.end > prev:
            prod_completion[a.product_id] = a.end
    recomputed = sum(
        max(0, round((prod_completion[p.id] - p.due).total_seconds() / 60))
        for p in req.products
        if p.id in prod_completion
    )
    assert abs(recomputed - kpis.tardiness_minutes) <= 1


def test_makespan_matches_assignments():
    result, kpis, _ = _run()
    earliest = min(a.start for a in result.assignments)
    latest   = max(a.end   for a in result.assignments)
    expected = round((latest - earliest).total_seconds() / 60)
    assert abs(kpis.makespan_minutes - expected) <= 1


def test_utilization_in_range():
    _, kpis, req = _run()
    for res_id, pct in kpis.utilization_pct.items():
        assert 0 <= pct <= 100, f"{res_id} utilization {pct}% out of range"


def test_utilization_excludes_changeover():
    """Numerator must be processing minutes only."""
    result, kpis, req = _run()
    for res in req.resources:
        processing = sum(
            round((a.end - a.start).total_seconds() / 60)
            for a in result.assignments if a.resource_id == res.id
        )
        available = round(res.available_minutes())
        expected = round(100 * processing / available) if available > 0 else 0
        assert abs(kpis.utilization_pct[res.id] - expected) <= 1, (
            f"{res.id}: expected {expected}%, reported {kpis.utilization_pct[res.id]}%"
        )


def test_changeover_count_and_minutes_consistent():
    result, kpis, _ = _run()
    total = sum(
        round((end - start).total_seconds() / 60)
        for _, start, end in result.changeover_intervals
    )
    assert total == kpis.changeover_minutes
    assert len(result.changeover_intervals) == kpis.changeover_count


def test_zero_tardiness_when_dues_generous():
    """All products finishing before generous due dates → 0 tardiness."""
    raw = copy.deepcopy(SAMPLE_INPUT)
    for p in raw["products"]:
        p["due"] = "2025-11-03T16:00:00"
    _, kpis, _ = _run(raw)
    assert kpis.tardiness_minutes == 0


def test_all_resources_in_utilization():
    _, kpis, req = _run()
    expected_ids = {r.id for r in req.resources}
    assert set(kpis.utilization_pct.keys()) == expected_ids


def test_validate_script_passes_on_sample(tmp_path):
    """The standalone validate.py must pass on the sample output."""
    import json, subprocess
    result, kpis, req = _run()

    assignments_out = [
        {
            "product": a.product_id, "step_index": a.step_index,
            "capability": a.capability, "resource": a.resource_id,
            "start": a.start.isoformat(), "end": a.end.isoformat(),
        }
        for a in result.assignments
    ]
    output = {
        "assignments": assignments_out,
        "kpis": {
            "tardiness_minutes":  kpis.tardiness_minutes,
            "changeover_count":   kpis.changeover_count,
            "changeover_minutes": kpis.changeover_minutes,
            "makespan_minutes":   kpis.makespan_minutes,
            "utilization_pct":    kpis.utilization_pct,
        },
    }
    out_file   = tmp_path / "output.json"
    input_file = tmp_path / "input.json"
    out_file.write_text(json.dumps(output))
    input_file.write_text(json.dumps(SAMPLE_INPUT))

    r = subprocess.run(
        ["python", "validate.py", str(out_file), str(input_file)],
        capture_output=True, text=True,
        cwd=os.path.join(os.path.dirname(__file__), "..")
    )
    assert r.returncode == 0, f"validate.py failed:\n{r.stdout}\n{r.stderr}"
