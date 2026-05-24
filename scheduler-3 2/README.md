# Harmony Production Scheduler

A constraint-based production scheduling API built with Python, FastAPI, and Google OR-Tools CP-SAT.

---

## How to Run

### Locally (Python 3.12+)

```bash
pip install -r requirements.txt
make run
# → http://localhost:8000
```

### Docker

```bash
make docker-build
make docker-run
# → http://localhost:8000
```

### Send a request

```bash
curl -s -X POST http://localhost:8000/schedule \
  -H "Content-Type: application/json" \
  -d @examples/client_a_sample.json | python -m json.tool
```

---

## How to Run Tests

```bash
make test
# or: python -m pytest tests/ -v
```

44 tests across four files:

| File | What it covers |
|---|---|
| `test_invariants.py` | No-overlap, precedence, calendar, horizon, changeover gaps, determinism, split-window compliance |
| `test_kpis.py` | Tardiness reproducibility, makespan, utilization (excludes changeover), validate.py integration |
| `test_infeasible.py` | Missing capability, window too short, horizon too short, structured error shape |
| `test_api.py` | Response shape, assignment count, min_makespan objective, 501 on unknown objective, solver warning |

### Standalone acceptance checker

```bash
# After running the service and capturing output:
curl -s -X POST http://localhost:8000/schedule \
  -H "Content-Type: application/json" \
  -d @examples/client_a_sample.json > output.json

python validate.py output.json examples/client_a_sample.json
# → PASS — all acceptance checks passed ✓
```

`validate.py` independently re-verifies every hard constraint (no-overlap, precedence, calendar, horizon, changeovers, capability eligibility, all steps present) and checks that all reported KPI values match recomputed values within ±1 minute.

### Visualization

```bash
# Generate Gantt chart + system flow diagram
python visualize.py output.json examples/client_a_sample.json
# Opens schedule_viz.html in your browser

# In Colab:
# from IPython.display import HTML
# HTML(open("schedule_viz.html").read())
```

`schedule_viz.html` contains two tabs:
- **Gantt Chart** — production schedule with color-coded families, changeover gaps, and due date markers
- **System Flow** — request flow through the codebase, color-coded by layer

---

## Approach

**Solver: Google OR-Tools CP-SAT**

Each `(product × step × eligible_resource)` triple gets an *optional* interval variable. The solver assigns each step to exactly one resource and chooses start times to minimise the objective.

### Key modelling decisions

**`AddCircuit` for sequencing and changeovers**  
For each resource, all operations are nodes in a circuit. Arc `i → j` (job i directly before job j) enforces `j.start ≥ i.end + changeover(family_i, family_j)`. This is the canonical OR-Tools approach — O(n log n) internally vs O(n²) for pairwise booleans, and naturally extracts the exact sequence for KPI reporting.

**Optional intervals with `AddExactlyOne`**  
Each step has one optional interval per eligible resource; exactly one presence boolean is true. Clean assignment without auxiliary variables.

**Calendar windows**  
Per `(step, resource)` pair: one boolean per window enforcing `start ≥ ws ∧ end ≤ we`; exactly one must be true when the step is assigned. Windows too short for the operation are silently skipped.

**Objective registry**  
Only `min_tardiness` is implemented per the spec. A second objective (e.g. `min_makespan`) would require:
1. Add the mode to `ObjectiveMode` in `model.py`
2. Write one `_build_<name>()` function in `scheduler.py`
3. Register it in `OBJECTIVE_BUILDERS`

Zero other files change.

**Determinism**  
`num_workers = 1` ensures identical input → identical output every run.

---

## Assumptions & Tradeoffs

| Decision | Rationale |
|---|---|
| CP-SAT over greedy | Exact optimal; greedy EDD can miss solutions requiring "wait for changeover" |
| `num_workers = 1` | Trades parallelism for determinism; fine for this instance size |
| `AddCircuit` | Scales to large instances; replaces O(n²) pairwise bool approach |
| Integer minutes | CP-SAT requires integer variables; sub-minute durations not supported (change `_to_min` granularity to add) |
| `solver_status` in response | Transparent to caller when time limit was hit and result may not be optimal |
| No DB / frontend | Per spec |

---

## Design Note

### Request flow

```
HTTP POST /schedule
  └─ app/api/routes.py            transport: raw JSON → errors mapped to HTTP codes
       └─ app/adapters/client_a.py  validate + translate Client A wire format → ScheduleRequest
            └─ app/core/scheduler.py    solve(): canonical model → ScheduleResult
            └─ app/core/kpis.py         compute_kpis(): ScheduleResult → KPIs
       └─ app/api/routes.py            serialize → JSON response
```

### Canonical internal model (`app/core/model.py`)

```
ScheduleRequest
  horizon_start / horizon_end : datetime
  resources    : List[Resource]        # id, capabilities (frozenset), calendar (List[Window])
  products     : List[Product]         # id, family, due, route (List[RouteStep])
  changeover_matrix : Dict[(str,str), int]   # (from_family, to_family) → minutes
  objective_mode : ObjectiveMode
  time_limit_seconds : int

ScheduleResult
  assignments          : List[Assignment]              # product_id, step_index, capability, resource_id, start, end
  changeover_intervals : List[(resource_id, start, end)]   # KPI use only
  solver_status        : str                           # "optimal" | "feasible_not_optimal"
```

The scheduler and KPI calculator depend **only** on these types. No client field name ever appears in `scheduler.py` or `kpis.py`.

### Where to add a second client input format

Create `app/adapters/client_b.py` with `parse(raw: dict) -> ScheduleRequest`.  
Add a route (or `?client=b` parameter) in `app/api/routes.py`.  
**Zero changes** to the scheduler, KPI calculator, model, or tests.

### Where to add a new objective mode

1. Add the mode to `ObjectiveMode` in `app/core/model.py`.
2. Define `_build_<name>(model, request, op_vars, cap_to_resources, horizon_end_min)` in `scheduler.py`.
3. Register it in `OBJECTIVE_BUILDERS` in `scheduler.py`.  
The adapter and HTTP layer pass `objective_mode` through unchanged.

### Where to add a new constraint (e.g. maintenance window / frozen zone)

1. Add the field to `ScheduleRequest` in `model.py` (e.g. `frozen_zones: List[Window]`).
2. Write `_constrain_frozen_zones(model, request, op_vars)` in `scheduler.py` and call it from `solve()`.
3. Populate the field in `app/adapters/client_a.py` (and future client adapters).
4. Add an invariant test in `tests/test_invariants.py`.
