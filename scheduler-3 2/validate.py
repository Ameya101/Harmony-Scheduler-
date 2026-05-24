#!/usr/bin/env python
"""
Standalone acceptance checker.

Usage:
    python validate.py output.json input.json

Reads the scheduler's JSON output and the original input, then verifies
every hard constraint and checks that reported KPIs match recomputed values.

Exit code 0 = all checks passed.
Exit code 1 = one or more violations found.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Dict, List, Tuple


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _check(condition: bool, msg: str, errors: List[str]):
    if not condition:
        errors.append(msg)


def validate(output: dict, input_data: dict) -> List[str]:
    errors: List[str] = []
    assignments = output.get("assignments", [])
    kpis        = output.get("kpis", {})

    if not assignments:
        errors.append("No assignments in output")
        return errors

    # ── Build lookup structures ──────────────────────────────────────────
    res_caps:     Dict[str, set]              = {}
    res_calendar: Dict[str, List[Tuple]]      = {}
    for r in input_data["resources"]:
        res_caps[r["id"]]     = set(r["capabilities"])
        res_calendar[r["id"]] = [(_dt(w[0]), _dt(w[1])) for w in r["calendar"]]

    prod_due:    Dict[str, datetime]    = {}
    prod_family: Dict[str, str]         = {}
    prod_route:  Dict[str, List[dict]]  = {}
    for p in input_data["products"]:
        prod_due[p["id"]]    = _dt(p["due"])
        prod_family[p["id"]] = p["family"]
        prod_route[p["id"]]  = p["route"]

    horizon_start = _dt(input_data["horizon"]["start"])
    horizon_end   = _dt(input_data["horizon"]["end"])

    co_values: Dict[Tuple[str, str], int] = {}
    for key, minutes in input_data["changeover_matrix_minutes"]["values"].items():
        parts = key.split("->")
        co_values[(parts[0].strip(), parts[1].strip())] = minutes

    # ── 1. Capability eligibility ────────────────────────────────────────
    for a in assignments:
        _check(
            a["capability"] in res_caps.get(a["resource"], set()),
            f"Eligibility: {a['resource']} lacks capability '{a['capability']}' "
            f"for {a['product']} step {a['step_index']}",
            errors,
        )

    # ── 2. No overlap per resource ───────────────────────────────────────
    by_res: Dict[str, list] = {}
    for a in assignments:
        by_res.setdefault(a["resource"], []).append(a)

    for res_id, ops in by_res.items():
        sorted_ops = sorted(ops, key=lambda x: _dt(x["start"]))
        for i in range(len(sorted_ops) - 1):
            cur, nxt = sorted_ops[i], sorted_ops[i + 1]
            _check(
                _dt(cur["end"]) <= _dt(nxt["start"]),
                f"Overlap on {res_id}: {cur['product']}[{cur['step_index']}] "
                f"ends {cur['end']} but {nxt['product']}[{nxt['step_index']}] starts {nxt['start']}",
                errors,
            )

    # ── 3. Precedence ────────────────────────────────────────────────────
    by_prod: Dict[str, list] = {}
    for a in assignments:
        by_prod.setdefault(a["product"], []).append(a)

    for prod_id, ops in by_prod.items():
        sorted_ops = sorted(ops, key=lambda x: x["step_index"])
        for i in range(len(sorted_ops) - 1):
            cur, nxt = sorted_ops[i], sorted_ops[i + 1]
            _check(
                _dt(cur["end"]) <= _dt(nxt["start"]),
                f"Precedence: {prod_id} step {cur['step_index']} ends {cur['end']} "
                f"but step {nxt['step_index']} starts {nxt['start']}",
                errors,
            )

    # ── 4. Calendar compliance ───────────────────────────────────────────
    for a in assignments:
        start_dt = _dt(a["start"])
        end_dt   = _dt(a["end"])
        windows  = res_calendar.get(a["resource"], [])
        fits     = any(ws <= start_dt and end_dt <= we for ws, we in windows)
        _check(
            fits,
            f"Calendar: {a['product']}[{a['step_index']}] on {a['resource']} "
            f"[{a['start']}, {a['end']}] fits no calendar window",
            errors,
        )

    # ── 5. Horizon bounds ────────────────────────────────────────────────
    for a in assignments:
        _check(
            _dt(a["start"]) >= horizon_start,
            f"Horizon: {a['product']} step {a['step_index']} starts before horizon",
            errors,
        )
        _check(
            _dt(a["end"]) <= horizon_end,
            f"Horizon: {a['product']} step {a['step_index']} ends after horizon",
            errors,
        )

    # ── 6. Changeover gaps ───────────────────────────────────────────────
    for res_id, ops in by_res.items():
        sorted_ops = sorted(ops, key=lambda x: _dt(x["start"]))
        for i in range(len(sorted_ops) - 1):
            cur, nxt = sorted_ops[i], sorted_ops[i + 1]
            fam_a    = prod_family.get(cur["product"], "")
            fam_b    = prod_family.get(nxt["product"], "")
            required = co_values.get((fam_a, fam_b), 0)
            if required > 0:
                gap = (_dt(nxt["start"]) - _dt(cur["end"])).total_seconds() / 60
                _check(
                    gap >= required - 0.01,  # 1-second tolerance
                    f"Changeover on {res_id}: {cur['product']}({fam_a}) → "
                    f"{nxt['product']}({fam_b}) needs {required} min gap, got {gap:.1f} min",
                    errors,
                )

    # ── 7. All products and steps present ────────────────────────────────
    scheduled_steps = {(a["product"], a["step_index"]) for a in assignments}
    for p in input_data["products"]:
        for s_idx, _ in enumerate(p["route"]):
            _check(
                (p["id"], s_idx + 1) in scheduled_steps,
                f"Missing: {p['id']} step {s_idx+1} not in assignments",
                errors,
            )

    # ── 8. KPI reproducibility ───────────────────────────────────────────
    # Tardiness
    prod_completion: Dict[str, datetime] = {}
    for a in assignments:
        prev = prod_completion.get(a["product"])
        end  = _dt(a["end"])
        if prev is None or end > prev:
            prod_completion[a["product"]] = end

    recomputed_tardiness = sum(
        max(0, round((_dt_val - prod_due[pid]).total_seconds() / 60))
        for pid, _dt_val in prod_completion.items()
        if pid in prod_due
    )
    reported_tardiness = kpis.get("tardiness_minutes", -1)
    _check(
        abs(recomputed_tardiness - reported_tardiness) <= 1,
        f"KPI mismatch — tardiness: reported {reported_tardiness}, recomputed {recomputed_tardiness}",
        errors,
    )

    # Makespan
    if assignments:
        earliest = min(_dt(a["start"]) for a in assignments)
        latest   = max(_dt(a["end"])   for a in assignments)
        recomputed_makespan = round((latest - earliest).total_seconds() / 60)
        reported_makespan   = kpis.get("makespan_minutes", -1)
        _check(
            abs(recomputed_makespan - reported_makespan) <= 1,
            f"KPI mismatch — makespan: reported {reported_makespan}, recomputed {recomputed_makespan}",
            errors,
        )

    # Utilization
    processing_by_res: Dict[str, int] = {}
    for a in assignments:
        dur = round((_dt(a["end"]) - _dt(a["start"])).total_seconds() / 60)
        processing_by_res[a["resource"]] = processing_by_res.get(a["resource"], 0) + dur

    for r in input_data["resources"]:
        available = sum(
            round((_dt(w[1]) - _dt(w[0])).total_seconds() / 60)
            for w in r["calendar"]
        )
        processed = processing_by_res.get(r["id"], 0)
        expected  = round(100 * processed / available) if available > 0 else 0
        reported  = kpis.get("utilization_pct", {}).get(r["id"], -1)
        _check(
            abs(expected - reported) <= 1,
            f"KPI mismatch — utilization {r['id']}: reported {reported}%, recomputed {expected}%",
            errors,
        )

    return errors


def main():
    if len(sys.argv) != 3:
        print("Usage: python validate.py output.json input.json")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        output = json.load(f)
    with open(sys.argv[2]) as f:
        input_data = json.load(f)

    errors = validate(output, input_data)

    if errors:
        print(f"FAIL — {len(errors)} violation(s) found:\n")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)
    else:
        print(f"PASS — all acceptance checks passed ✓")
        sys.exit(0)


if __name__ == "__main__":
    main()
