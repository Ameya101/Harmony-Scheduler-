"""
Canonical internal scheduling model.

This module owns the typed domain objects that the scheduler, KPI calculator,
and constraint checker all operate on.  Nothing here knows about HTTP, JSON, or
any client-specific field names.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple


class ObjectiveMode(str, Enum):
    MIN_TARDINESS = "min_tardiness"
    MIN_MAKESPAN  = "min_makespan"


@dataclass(frozen=True)
class Window:
    """A contiguous block of time when a resource is available."""
    start: datetime
    end: datetime

    def duration_minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    def contains(self, start: datetime, end: datetime) -> bool:
        return self.start <= start and end <= self.end


@dataclass(frozen=True)
class ChangeoverMatrix:
    """
    Lookup table for setup minutes between consecutive families on the same resource.

    Example:
        matrix.lookup("standard", "premium")  # returns 20
        matrix.lookup("standard", "standard") # returns 0
    """
    values: Dict[Tuple[str, str], int]

    def lookup(self, from_family: str, to_family: str) -> int:
        return self.values.get((from_family, to_family), 0)

    def all_pairs_present(self, families: List[str]) -> List[str]:
        """Return list of missing (from, to) pair strings for validation."""
        missing = []
        for fa in sorted(families):
            for fb in sorted(families):
                if (fa, fb) not in self.values:
                    missing.append(f"'{fa}->{fb}'")
        return missing


@dataclass(frozen=True)
class Resource:
    id: str
    capabilities: frozenset
    calendar: List[Window]

    def available_minutes(self) -> float:
        return sum(w.duration_minutes() for w in self.calendar)

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class RouteStep:
    capability: str
    duration_minutes: int


@dataclass(frozen=True)
class Product:
    id: str
    family: str
    due: datetime
    route: List[RouteStep]


@dataclass
class ScheduleRequest:
    horizon_start: datetime
    horizon_end: datetime
    resources: List[Resource]
    products: List[Product]
    changeover_matrix: ChangeoverMatrix
    objective_mode: ObjectiveMode
    time_limit_seconds: int


@dataclass(frozen=True)
class Assignment:
    product_id: str
    step_index: int
    capability: str
    resource_id: str
    start: datetime
    end: datetime


@dataclass
class ScheduleResult:
    assignments: List[Assignment]
    changeover_intervals: List[Tuple[str, datetime, datetime]]
    solver_status: str = "optimal"


@dataclass
class KPIs:
    tardiness_minutes: int
    changeover_count: int
    changeover_minutes: int
    makespan_minutes: int
    utilization_pct: Dict[str, int]
