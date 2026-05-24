"""
Client A input adapter.

Responsibility: validate the raw Client A JSON body and translate it into
the canonical ScheduleRequest.

To support Client B:
    1. Create app/adapters/client_b.py with the same signature:
           parse(raw: dict) -> ScheduleRequest
    2. Register / route it in app/api/routes.py.
    The core scheduler, KPI calculator, and all tests need zero changes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.model import (
    ChangeoverMatrix,
    ObjectiveMode,
    Product,
    Resource,
    RouteStep,
    ScheduleRequest,
    Window,
)


# ── Pydantic wire-format schemas ─────────────────────────────────────────────

class _Horizon(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def start_before_end(self):
        if self.start >= self.end:
            raise ValueError("horizon.start must be before horizon.end")
        return self


class _ResourceIn(BaseModel):
    id: str
    capabilities: List[str]
    calendar: List[Tuple[datetime, datetime]]

    @field_validator("capabilities")
    @classmethod
    def non_empty_caps(cls, v):
        if not v:
            raise ValueError("capabilities must not be empty")
        return v

    @field_validator("calendar")
    @classmethod
    def windows_sorted_non_overlapping(cls, v):
        sorted_v = sorted(v, key=lambda w: w[0])
        for i in range(len(sorted_v) - 1):
            if sorted_v[i][1] > sorted_v[i + 1][0]:
                raise ValueError("calendar windows overlap")
        return sorted_v


class _RouteStepIn(BaseModel):
    capability: str
    duration_minutes: int = Field(gt=0)


class _ProductIn(BaseModel):
    id: str
    family: str
    due: datetime
    route: List[_RouteStepIn] = Field(min_length=1)


class _ChangeoverMatrix(BaseModel):
    values: Dict[str, int]


class _Settings(BaseModel):
    time_limit_seconds: int = Field(default=30, gt=0)
    objective_mode: str = "min_tardiness"

    @field_validator("objective_mode")
    @classmethod
    def known_mode(cls, v):
        try:
            ObjectiveMode(v)
        except ValueError:
            valid = [m.value for m in ObjectiveMode]
            raise ValueError(f"Unknown objective_mode '{v}'. Valid: {valid}")
        return v


class ClientARequest(BaseModel):
    horizon: _Horizon
    resources: List[_ResourceIn] = Field(min_length=1)
    changeover_matrix_minutes: _ChangeoverMatrix
    products: List[_ProductIn] = Field(min_length=1)
    settings: _Settings = Field(default_factory=_Settings)


# ── Adapter entry point ──────────────────────────────────────────────────────

def parse(raw: Dict[str, Any]) -> ScheduleRequest:
    """
    Validate and translate Client A wire JSON → canonical ScheduleRequest.
    Raises pydantic.ValidationError on bad input.
    """
    req = ClientARequest.model_validate(raw)

    resources = [
        Resource(
            id=r.id,
            capabilities=frozenset(r.capabilities),
            calendar=[Window(start=w[0], end=w[1]) for w in r.calendar],
        )
        for r in req.resources
    ]

    products = [
        Product(
            id=p.id,
            family=p.family,
            due=p.due,
            route=[
                RouteStep(capability=s.capability, duration_minutes=s.duration_minutes)
                for s in p.route
            ],
        )
        for p in req.products
    ]

    _co_values: Dict[Tuple[str, str], int] = {}
    for key, minutes in req.changeover_matrix_minutes.values.items():
        parts = key.split("->")
        if len(parts) != 2:
            raise ValueError(f"Invalid changeover key '{key}': expected 'family->family'")
        _co_values[(parts[0].strip(), parts[1].strip())] = minutes
    changeover_matrix = ChangeoverMatrix(values=_co_values)

    _validate_changeover_completeness(changeover_matrix, products)

    return ScheduleRequest(
        horizon_start=req.horizon.start,
        horizon_end=req.horizon.end,
        resources=resources,
        products=products,
        changeover_matrix=changeover_matrix,
        objective_mode=ObjectiveMode(req.settings.objective_mode),
        time_limit_seconds=req.settings.time_limit_seconds,
    )


def _validate_changeover_completeness(
    changeover_matrix: ChangeoverMatrix,
    products: list,
):
    """
    FIX #3: Fail explicitly if any family pair is missing from the matrix.
    Uses ChangeoverMatrix.all_pairs_present() for clean validation.
    """
    all_families = list({p.family for p in products})
    missing = changeover_matrix.all_pairs_present(all_families)
    if missing:
        raise ValueError(
            f"changeover_matrix_minutes is missing entries: {', '.join(missing)}. "
            f"All family pairs (including same-family) must be specified."
        )
