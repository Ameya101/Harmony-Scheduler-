"""
KPI calculator.

Operates on ScheduleResult + ScheduleRequest (canonical models only).
No dependency on HTTP, JSON, or client schemas.

To add a new KPI:
    1. Add the field to KPIs dataclass in model.py
    2. Add the calculation here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict

from .model import KPIs, ScheduleRequest, ScheduleResult


def compute_kpis(result: ScheduleResult, request: ScheduleRequest) -> KPIs:
    assignments = result.assignments

    if not assignments:
        return KPIs(
            tardiness_minutes=0,
            changeover_count=0,
            changeover_minutes=0,
            makespan_minutes=0,
            utilization_pct={r.id: 0 for r in request.resources},
        )

    # ── Tardiness ────────────────────────────────────────────────────────
    prod_completion: Dict[str, datetime] = {}
    for a in assignments:
        prev = prod_completion.get(a.product_id)
        if prev is None or a.end > prev:
            prod_completion[a.product_id] = a.end

    total_tardiness = sum(
        max(0, round((prod_completion[p.id] - p.due).total_seconds() / 60))
        for p in request.products
        if p.id in prod_completion
    )

    # ── Changeovers ──────────────────────────────────────────────────────
    changeover_count   = len(result.changeover_intervals)
    changeover_minutes = sum(
        round((end - start).total_seconds() / 60)
        for _, start, end in result.changeover_intervals
    )

    # ── Makespan ─────────────────────────────────────────────────────────
    earliest_start  = min(a.start for a in assignments)
    latest_end      = max(a.end   for a in assignments)
    makespan_minutes = round((latest_end - earliest_start).total_seconds() / 60)

    # ── Utilization ──────────────────────────────────────────────────────
    # Numerator: processing minutes only (changeover excluded per spec)
    processing_by_res: Dict[str, int] = {}
    for a in assignments:
        dur = round((a.end - a.start).total_seconds() / 60)
        processing_by_res[a.resource_id] = processing_by_res.get(a.resource_id, 0) + dur

    utilization_pct: Dict[str, int] = {}
    for res in request.resources:
        available = round(res.available_minutes())
        processed = processing_by_res.get(res.id, 0)
        utilization_pct[res.id] = (
            round(100 * processed / available) if available > 0 else 0
        )

    return KPIs(
        tardiness_minutes=total_tardiness,
        changeover_count=changeover_count,
        changeover_minutes=changeover_minutes,
        makespan_minutes=makespan_minutes,
        utilization_pct=utilization_pct,
    )
