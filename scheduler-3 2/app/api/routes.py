"""
HTTP transport layer.

Responsibilities:
  - Route HTTP requests to the correct adapter + scheduler
  - Serialize ScheduleResult / KPIs back to JSON
  - Map domain errors to HTTP status codes

No scheduling logic lives here.
Adding Client B: add one route, import client_b.parse, reuse solve() + compute_kpis().
"""
from __future__ import annotations

from typing import Any, Dict

import fastapi
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.adapters.client_a import parse as parse_client_a
from app.core import InfeasibilityError, compute_kpis, solve

router = fastapi.APIRouter()


@router.post("/schedule")
async def schedule(request: Request) -> JSONResponse:
    raw = await request.json()

    # 1. Parse & validate (Client A adapter)
    try:
        sched_request = parse_client_a(raw)
    except ValidationError as exc:
        details = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"error": "validation_error", "detail": details})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": "validation_error", "detail": str(exc)})

    # 2. Solve
    try:
        result = solve(sched_request)
    except InfeasibilityError as exc:
        return JSONResponse(status_code=422, content={"error": "infeasible", "why": exc.reasons})
    except NotImplementedError as exc:
        return JSONResponse(status_code=501, content={"error": "not_implemented", "detail": str(exc)})

    # 3. KPIs
    kpis = compute_kpis(result, sched_request)

    # 4. Serialize
    assignments_out = [
        {
            "product":    a.product_id,
            "step_index": a.step_index,
            "capability": a.capability,
            "resource":   a.resource_id,
            "start":      a.start.isoformat(),
            "end":        a.end.isoformat(),
        }
        for a in result.assignments
    ]

    kpis_out: Dict[str, Any] = {
        "tardiness_minutes":  kpis.tardiness_minutes,
        "changeover_count":   kpis.changeover_count,
        "changeover_minutes": kpis.changeover_minutes,
        "makespan_minutes":   kpis.makespan_minutes,
        "utilization_pct":    kpis.utilization_pct,
    }

    response_body: Dict[str, Any] = {
        "assignments":   assignments_out,
        "kpis":          kpis_out,
        "solver_status": result.solver_status,
    }

    # Surface a warning if the solver hit the time limit
    if result.solver_status == "feasible_not_optimal":
        response_body["warning"] = (
            "Time limit reached; a feasible schedule was found but it may not be optimal. "
            "Increase settings.time_limit_seconds for a better result."
        )

    return JSONResponse(status_code=200, content=response_body)


@router.get("/health")
async def health():
    return JSONResponse(status_code=200, content={"status": "ok"})
