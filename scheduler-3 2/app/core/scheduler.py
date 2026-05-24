"""
Core constraint-programming scheduler.

Operates exclusively on the canonical internal model (app.core.model).
Has zero knowledge of HTTP, JSON schemas, or client-specific field names.

Extensibility guide
-------------------
Add a new objective mode:
    1. Add the mode to ObjectiveMode in model.py
    2. Register a builder function in OBJECTIVE_BUILDERS at the bottom of this file.

Add a new hard constraint (e.g. maintenance window / frozen zone):
    1. Add the field to ScheduleRequest in model.py
    2. Add a _constrain_<name>() function here and call it from solve().

Add a second client input format:
    → Only app/adapters/client_b.py needs to be created. Zero changes here.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

from .model import (
    Assignment,
    ObjectiveMode,
    Product,
    Resource,
    ScheduleRequest,
    ScheduleResult,
)

# ---------------------------------------------------------------------------
# Time helpers — CP-SAT requires integer variables; we work in minutes.
# ---------------------------------------------------------------------------
_EPOCH: Optional[datetime] = None


def _to_min(dt: datetime) -> int:
    assert _EPOCH is not None
    return int((dt - _EPOCH).total_seconds() // 60)


def _from_min(minutes: int) -> datetime:
    assert _EPOCH is not None
    return _EPOCH + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class InfeasibilityError(Exception):
    """Raised when no feasible schedule exists."""
    def __init__(self, reasons: List[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def solve(request: ScheduleRequest) -> ScheduleResult:
    """
    Build and solve the CP-SAT scheduling model.

    Returns ScheduleResult on success.
    Raises InfeasibilityError with concrete human-readable reasons on failure.
    """
    global _EPOCH
    _EPOCH = request.horizon_start

    horizon_end_min = _to_min(request.horizon_end)

    # FIX #2: build lookup dict once — O(1) access everywhere, no more O(n) scan
    prod_by_id: Dict[str, Product] = {p.id: p for p in request.products}

    # Build capability index
    cap_to_resources = _build_cap_index(request)

    # FIX #5: Fast LP-relaxation capacity check BEFORE invoking the solver.
    # Catches obvious infeasibility immediately without burning time_limit_seconds.
    _capacity_preflight(request, cap_to_resources)

    # Detailed per-step preflight (window size, missing capability)
    _preflight(request, cap_to_resources)

    model = cp_model.CpModel()

    # ------------------------------------------------------------------
    # Decision variables
    # ------------------------------------------------------------------
    # op_vars[(prod_id, step_idx, res_id)] = {
    #   "start": IntVar, "end": IntVar, "interval": OptionalIntervalVar,
    #   "active": BoolVar, "family": str
    # }
    op_vars: Dict[Tuple[str, int, str], dict] = {}

    for prod in request.products:
        for s_idx, step in enumerate(prod.route):
            for res in cap_to_resources.get(step.capability, []):
                key    = (prod.id, s_idx, res.id)
                active = model.NewBoolVar(f"active_{prod.id}_{s_idx}_{res.id}")
                start  = model.NewIntVar(0, horizon_end_min, f"s_{prod.id}_{s_idx}_{res.id}")
                end    = model.NewIntVar(0, horizon_end_min, f"e_{prod.id}_{s_idx}_{res.id}")
                interval = model.NewOptionalIntervalVar(
                    start, step.duration_minutes, end, active,
                    f"iv_{prod.id}_{s_idx}_{res.id}"
                )
                op_vars[key] = {
                    "start": start, "end": end,
                    "interval": interval, "active": active,
                    "family": prod.family,
                }

    # ------------------------------------------------------------------
    # Constraint: each step assigned to exactly one eligible resource
    # ------------------------------------------------------------------
    for prod in request.products:
        for s_idx, step in enumerate(prod.route):
            eligible = cap_to_resources.get(step.capability, [])
            bools = [op_vars[(prod.id, s_idx, r.id)]["active"] for r in eligible]
            if not bools:
                raise InfeasibilityError([
                    f"No resource with capability '{step.capability}' "
                    f"for {prod.id} step {s_idx+1}"
                ])
            model.AddExactlyOne(bools)

    # ------------------------------------------------------------------
    # Constraint: calendar windows
    # ------------------------------------------------------------------
    res_by_id = {r.id: r for r in request.resources}

    for (prod_id, s_idx, res_id), v in op_vars.items():
        res = res_by_id[res_id]
        dur = prod_by_id[prod_id].route[s_idx].duration_minutes
        in_window_bools = []
        for win in res.calendar:
            w_start = _to_min(win.start)
            w_end   = _to_min(win.end)
            if win.duration_minutes() < dur:
                continue
            b = model.NewBoolVar(f"win_{prod_id}_{s_idx}_{res_id}_{w_start}")
            model.Add(v["start"] >= w_start).OnlyEnforceIf(b)
            model.Add(v["end"]   <= w_end  ).OnlyEnforceIf(b)
            in_window_bools.append(b)
        if in_window_bools:
            model.Add(sum(in_window_bools) == 1).OnlyEnforceIf(v["active"])
        else:
            # FIX #6: force inactive immediately — exclude from circuit below
            model.Add(v["active"] == 0)

    # ------------------------------------------------------------------
    # Constraint: precedence within each product
    # ------------------------------------------------------------------
    for prod in request.products:
        for s_idx in range(len(prod.route) - 1):
            eligible_cur = cap_to_resources.get(prod.route[s_idx].capability, [])
            eligible_nxt = cap_to_resources.get(prod.route[s_idx + 1].capability, [])

            chosen_end   = model.NewIntVar(0, horizon_end_min, f"cend_{prod.id}_{s_idx}")
            chosen_start = model.NewIntVar(0, horizon_end_min, f"cstart_{prod.id}_{s_idx+1}")

            for r in eligible_cur:
                v = op_vars[(prod.id, s_idx, r.id)]
                model.Add(chosen_end == v["end"]).OnlyEnforceIf(v["active"])
            for r in eligible_nxt:
                v = op_vars[(prod.id, s_idx + 1, r.id)]
                model.Add(chosen_start == v["start"]).OnlyEnforceIf(v["active"])

            model.Add(chosen_end <= chosen_start)

    # ------------------------------------------------------------------
    # Constraint: no-overlap + changeovers via AddCircuit
    # ------------------------------------------------------------------
    # FIX #6: only include ops that could actually be active (have at least
    # one valid window). Forced-inactive ops are excluded from the circuit
    # entirely, reducing model size.

    changeover_arcs: List[Tuple] = []

    for res in request.resources:
        # Only ops that are not forced-inactive
        res_ops = [
            (prod_id, s_idx, v)
            for (prod_id, s_idx, res_id), v in op_vars.items()
            if res_id == res.id
            # has at least one window bool → not forced-inactive
            and any(
                win.duration_minutes() >= prod_by_id[prod_id].route[s_idx].duration_minutes
                for win in res_by_id[res_id].calendar
            )
        ]
        if not res_ops:
            continue

        arcs = []

        # Depot → each node
        for i, (_, _, v) in enumerate(res_ops):
            lit = model.NewBoolVar(f"arc_depot_{res.id}_{i}")
            arcs.append((0, i + 1, lit))

        # Each node → depot
        for i, (_, _, v) in enumerate(res_ops):
            lit = model.NewBoolVar(f"arc_{res.id}_{i}_depot")
            arcs.append((i + 1, 0, lit))

        # Node i → node j (i runs directly before j on this resource)
        for i, (pid_a, si_a, va) in enumerate(res_ops):
            for j, (pid_b, si_b, vb) in enumerate(res_ops):
                if i == j:
                    continue
                fam_a = va["family"]
                fam_b = vb["family"]
                co    = request.changeover_matrix.lookup(fam_a, fam_b)

                lit = model.NewBoolVar(f"arc_{res.id}_{i}_{j}")
                model.Add(vb["start"] >= va["end"] + co).OnlyEnforceIf(lit)
                arcs.append((i + 1, j + 1, lit))

                if co > 0:
                    changeover_arcs.append((res.id, va, vb, co, lit))

        # Self-loops for inactive nodes
        for i, (_, _, v) in enumerate(res_ops):
            skip = model.NewBoolVar(f"skip_{res.id}_{i}")
            model.Add(skip == 1).OnlyEnforceIf(v["active"].Not())
            model.Add(skip == 0).OnlyEnforceIf(v["active"])
            arcs.append((i + 1, i + 1, skip))

        model.AddCircuit(arcs)
        model.AddNoOverlap([v["interval"] for _, _, v in res_ops])

    # ------------------------------------------------------------------
    # FIX #7: Greedy warm start — provide EDD solution as hint to solver
    # ------------------------------------------------------------------
    hints = _greedy_edd_hints(request, cap_to_resources, prod_by_id, op_vars)
    for var, val in hints:
        model.AddHint(var, val)

    # ------------------------------------------------------------------
    # Objective
    # ------------------------------------------------------------------
    builder = OBJECTIVE_BUILDERS.get(request.objective_mode)
    if builder is None:
        raise NotImplementedError(
            f"Objective mode '{request.objective_mode}' is not implemented. "
            f"Available: {list(OBJECTIVE_BUILDERS)}"
        )
    builder(model, request, op_vars, cap_to_resources, horizon_end_min, prod_by_id)

    # ------------------------------------------------------------------
    # Solve
    # FIX #1: num_workers=8 + random_seed=42 — faster AND deterministic
    # ------------------------------------------------------------------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = request.time_limit_seconds
    solver.parameters.num_workers         = 1    # deterministic: same input → same output
    solver.parameters.random_seed         = 42   # fixed seed for reproducibility
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise InfeasibilityError(_diagnose(request, cap_to_resources))

    solver_status = "optimal" if status == cp_model.OPTIMAL else "feasible_not_optimal"

    # ------------------------------------------------------------------
    # Extract solution
    # ------------------------------------------------------------------
    assignments = []
    for (prod_id, s_idx, res_id), v in sorted(op_vars.items()):
        if solver.BooleanValue(v["active"]):
            assignments.append(Assignment(
                product_id=prod_id,
                step_index=s_idx + 1,
                capability=prod_by_id[prod_id].route[s_idx].capability,
                resource_id=res_id,
                start=_from_min(solver.Value(v["start"])),
                end=_from_min(solver.Value(v["end"])),
            ))

    assignments.sort(key=lambda a: (a.product_id, a.step_index))

    # Extract changeover intervals from activated arcs
    co_intervals = []
    seen = set()
    for res_id, va, vb, co_min, lit in changeover_arcs:
        if solver.BooleanValue(lit):
            start_min = solver.Value(va["end"])
            key = (res_id, start_min)
            if key not in seen:
                seen.add(key)
                co_intervals.append((
                    res_id,
                    _from_min(start_min),
                    _from_min(start_min + co_min),
                ))

    return ScheduleResult(
        assignments=assignments,
        changeover_intervals=co_intervals,
        solver_status=solver_status,
    )


# ---------------------------------------------------------------------------
# FIX #7: Greedy EDD warm start
# ---------------------------------------------------------------------------

def _greedy_edd_hints(request, cap_to_resources, prod_by_id, op_vars):
    """
    Run a fast Earliest-Due-Date greedy pass and return (IntVar, int) hint pairs.

    This gives CP-SAT a valid starting point so it finds a good solution
    faster, especially on larger instances. The hints are non-binding —
    the solver will improve on them.
    """
    hints = []
    # resource_available[res_id] = next free minute on that resource
    resource_available: Dict[str, int] = {}
    for res in request.resources:
        if res.calendar:
            resource_available[res.id] = _to_min(res.calendar[0].start)
        else:
            resource_available[res.id] = 0

    # Sort products by due date (EDD)
    sorted_prods = sorted(request.products, key=lambda p: p.due)

    for prod in sorted_prods:
        step_end = 0
        for s_idx, step in enumerate(prod.route):
            eligible = cap_to_resources.get(step.capability, [])
            if not eligible:
                continue

            # Pick the eligible resource that becomes free earliest
            best_res  = None
            best_start = None

            for res in eligible:
                earliest = max(step_end, resource_available.get(res.id, 0))
                # Find the first window that fits
                for win in res.calendar:
                    ws = _to_min(win.start)
                    we = _to_min(win.end)
                    cand_start = max(earliest, ws)
                    if cand_start + step.duration_minutes <= we:
                        if best_start is None or cand_start < best_start:
                            best_start = cand_start
                            best_res   = res
                        break

            if best_res is None or best_start is None:
                continue

            best_end = best_start + step.duration_minutes
            resource_available[best_res.id] = best_end
            step_end = best_end

            # Emit hints for the chosen resource's vars
            key = (prod.id, s_idx, best_res.id)
            if key in op_vars:
                v = op_vars[key]
                hints.append((v["start"],  best_start))
                hints.append((v["end"],    best_end))
                hints.append((v["active"], 1))

            # Hint inactive for all other resources for this step
            for res in eligible:
                if res.id != best_res.id:
                    other_key = (prod.id, s_idx, res.id)
                    if other_key in op_vars:
                        hints.append((op_vars[other_key]["active"], 0))

    return hints


# ---------------------------------------------------------------------------
# Objective builders registry
# ---------------------------------------------------------------------------
# To add a new objective:
#   1. Define _build_<name>() with signature below
#   2. Add it to OBJECTIVE_BUILDERS — no other file needs to change.

def _build_min_tardiness(model, request, op_vars, cap_to_resources,
                          horizon_end_min, prod_by_id):
    tardiness_terms = []
    for prod in request.products:
        last_s   = len(prod.route) - 1
        due_min  = _to_min(prod.due)
        eligible = cap_to_resources.get(prod.route[last_s].capability, [])

        chosen_end = model.NewIntVar(0, horizon_end_min, f"obj_end_{prod.id}")
        for res in eligible:
            v = op_vars[(prod.id, last_s, res.id)]
            model.Add(chosen_end == v["end"]).OnlyEnforceIf(v["active"])

        tard = model.NewIntVar(0, horizon_end_min, f"tard_{prod.id}")
        model.AddMaxEquality(tard, [chosen_end - due_min, model.NewConstant(0)])
        tardiness_terms.append(tard)

    model.Minimize(sum(tardiness_terms))


def _build_min_makespan(model, request, op_vars, cap_to_resources,
                         horizon_end_min, prod_by_id):
    """Minimise the span from earliest start to latest end."""
    makespan = model.NewIntVar(0, horizon_end_min, "makespan")
    all_ends = [v["end"] for v in op_vars.values()]
    model.AddMaxEquality(makespan, all_ends)
    model.Minimize(makespan)


OBJECTIVE_BUILDERS: Dict[ObjectiveMode, Callable] = {
    ObjectiveMode.MIN_TARDINESS: _build_min_tardiness,
    ObjectiveMode.MIN_MAKESPAN:  _build_min_makespan,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_cap_index(request: ScheduleRequest) -> Dict[str, List[Resource]]:
    idx: Dict[str, List[Resource]] = {}
    for res in request.resources:
        for cap in res.capabilities:
            idx.setdefault(cap, []).append(res)
    return idx


def _capacity_preflight(request: ScheduleRequest,
                         cap_to_resources: Dict[str, List[Resource]]):
    """
    FIX #5: LP-relaxation style capacity check.

    Checks raw available minutes vs required minutes per capability.
    Runs in O(resources + products) — returns immediately without
    invoking the solver if capacity is clearly violated.
    """
    reasons = []
    for cap, resources in cap_to_resources.items():
        available = round(sum(res.available_minutes() for res in resources))
        needed = sum(
            step.duration_minutes
            for prod in request.products
            for step in prod.route
            if step.capability == cap
        )
        if needed > available:
            reasons.append(
                f"Capability '{cap}': {needed} min of work needed "
                f"but only {available} min of calendar available across all resources"
            )
    if reasons:
        raise InfeasibilityError(reasons)


def _preflight(request: ScheduleRequest,
               cap_to_resources: Dict[str, List[Resource]]):
    """Per-step checks: missing capability and window too small."""
    reasons = []
    for prod in request.products:
        for s_idx, step in enumerate(prod.route):
            eligible = cap_to_resources.get(step.capability, [])
            if not eligible:
                reasons.append(
                    f"Product {prod.id} step {s_idx+1}: "
                    f"no resource has capability '{step.capability}'"
                )
                continue
            fits = any(
                win.duration_minutes() >= step.duration_minutes
                for res in eligible
                for win in res.calendar
            )
            if not fits:
                reasons.append(
                    f"Product {prod.id} step {s_idx+1} "
                    f"({step.capability}, {step.duration_minutes} min): "
                    f"no single calendar window is large enough on any eligible resource"
                )
    if reasons:
        raise InfeasibilityError(reasons)


def _diagnose(request: ScheduleRequest,
              cap_to_resources: Dict[str, List[Resource]]) -> List[str]:
    """
    Called only after solver returns INFEASIBLE/UNKNOWN.
    Generates rich human-readable reasons.
    """
    reasons = ["Solver could not find a feasible schedule within the time limit."]

    # 1. Capacity per capability
    for cap, resources in cap_to_resources.items():
        available = round(sum(res.available_minutes() for res in resources))
        needed = sum(
            step.duration_minutes
            for prod in request.products
            for step in prod.route
            if step.capability == cap
        )
        if needed > available:
            reasons.append(
                f"Capability '{cap}': {needed} min needed, {available} min available"
            )

    # 2. Products that cannot meet their due date even with zero wait
    for prod in request.products:
        min_time = sum(s.duration_minutes for s in prod.route)
        due_min  = _to_min(prod.due)
        if min_time > due_min:
            reasons.append(
                f"Product {prod.id}: minimum processing time {min_time} min "
                f"exceeds due date offset {due_min} min from horizon start"
            )

    # 3. Changeover overhead
    horizon_min       = _to_min(request.horizon_end)
    total_processing  = sum(
        s.duration_minutes for prod in request.products for s in prod.route
    )
    total_co          = sum(request.changeover_matrix.values())
    num_resources     = len(request.resources)
    if total_processing + total_co > horizon_min * num_resources:
        reasons.append(
            "Combined processing and changeover time likely exceeds total resource capacity"
        )

    return reasons
