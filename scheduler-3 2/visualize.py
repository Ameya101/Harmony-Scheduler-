#!/usr/bin/env python
"""
Harmony Scheduler — Combined Visualizer

Generates a single HTML page with:
  1. System flow diagram  — how a request flows through the codebase
  2. Gantt chart          — the resulting production schedule
  3. KPI summary          — tardiness, makespan, utilization

Usage:
    # Run the scheduler first, save output
    curl -s -X POST http://localhost:8000/schedule \
      -H "Content-Type: application/json" \
      -d @examples/client_a_sample.json > output.json

    # Generate visualization
    python visualize.py output.json examples/client_a_sample.json

    # In Colab:
    from IPython.display import HTML
    HTML(open("schedule_viz.html").read())
"""
from __future__ import annotations

import json
import os
import sys
import webbrowser
from datetime import datetime
from typing import Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)

def _min_from(origin: datetime, dt: datetime) -> float:
    return (dt - origin).total_seconds() / 60


# ─────────────────────────────────────────────────────────────────────────────
# 1. Flow Diagram SVG
# ─────────────────────────────────────────────────────────────────────────────

def build_flow_diagram() -> str:
    W, H = 860, 520

    # Node definitions: (id, x, y, w, h, label, sublabel, color, shape)
    nodes = [
        # HTTP layer
        ("http",    380,  20, 160,  44, "POST /schedule",       "HTTP Request",            "#6366F1", "rect"),
        ("routes",  380,  90, 160,  44, "routes.py",            "Transport layer",         "#6366F1", "rect"),
        # Adapter layer
        ("adapter", 380, 165, 160,  44, "client_a.py",          "Adapter · validates JSON","#0EA5E9", "rect"),
        ("clientb", 600, 165, 130,  44, "client_b.py",          "Future adapter",          "#0EA5E9", "rect_dash"),
        # Canonical model
        ("model",   380, 240, 160,  44, "ScheduleRequest",      "Canonical model",         "#10B981", "rect"),
        # Scheduler
        ("sched",   380, 315, 160,  44, "scheduler.py",         "CP-SAT · OR-Tools",       "#F59E0B", "rect"),
        # Result + KPIs split
        ("result",  240, 390, 140,  44, "ScheduleResult",       "Assignments",             "#10B981", "rect"),
        ("kpis",    520, 390, 140,  44, "kpis.py",              "KPI calculator",          "#F59E0B", "rect"),
        # Response
        ("resp",    380, 465, 160,  44, "JSON Response",        "assignments + kpis",      "#6366F1", "rect"),
    ]

    # Edges: (from_id, to_id, label, style)
    edges = [
        ("http",    "routes",  "",                    "solid"),
        ("routes",  "adapter", "parse + validate",    "solid"),
        ("adapter", "model",   "→ ScheduleRequest",   "solid"),
        ("clientb", "model",   "same output",         "dashed"),
        ("model",   "sched",   "solve(request)",      "solid"),
        ("sched",   "result",  "",                    "solid"),
        ("sched",   "kpis",    "",                    "solid"),
        ("result",  "resp",    "",                    "solid"),
        ("kpis",    "resp",    "",                    "solid"),
    ]

    node_map = {n[0]: n for n in nodes}

    def cx(n): return n[1] + n[3] // 2
    def cy(n): return n[2] + n[4] // 2
    def right(n): return n[1] + n[3]
    def bottom(n): return n[2] + n[4]
    def top(n): return n[2]
    def left(n): return n[1]

    parts = []
    parts.append(
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        f'background:#F8FAFC;border-radius:12px;width:100%">'
    )

    # ── Edges ────────────────────────────────────────────────────────────────
    for (fid, tid, label, style) in edges:
        fn, tn = node_map[fid], node_map[tid]
        # Determine best connection points
        fx, fy = cx(fn), bottom(fn)
        tx, ty = cx(tn), top(tn)

        # Special cases for side connections
        if fid == "clientb" and tid == "model":
            fx, fy = left(fn), cy(fn)
            tx, ty = right(tn), cy(tn)
        elif fid == "sched" and tid == "result":
            fx, fy = cx(fn) - 20, bottom(fn)
            tx, ty = cx(tn) + 20, top(tn)
        elif fid == "sched" and tid == "kpis":
            fx, fy = cx(fn) + 20, bottom(fn)
            tx, ty = cx(tn) - 20, top(tn)
        elif fid == "result" and tid == "resp":
            fx, fy = cx(fn) + 10, bottom(fn)
            tx, ty = cx(tn) - 30, top(tn)
        elif fid == "kpis" and tid == "resp":
            fx, fy = cx(fn) - 10, bottom(fn)
            tx, ty = cx(tn) + 30, top(tn)

        dash = "stroke-dasharray='6,4'" if style == "dashed" else ""
        mid_x, mid_y = (fx + tx) // 2, (fy + ty) // 2

        parts.append(
            f'<defs><marker id="arr_{fid}_{tid}" markerWidth="8" markerHeight="6" '
            f'refX="8" refY="3" orient="auto">'
            f'<polygon points="0 0, 8 3, 0 6" fill="#94A3B8"/></marker></defs>'
        )
        parts.append(
            f'<path d="M{fx},{fy} C{fx},{fy+30} {tx},{ty-30} {tx},{ty}" '
            f'fill="none" stroke="#94A3B8" stroke-width="1.5" {dash} '
            f'marker-end="url(#arr_{fid}_{tid})"/>'
        )
        if label:
            parts.append(
                f'<text x="{mid_x}" y="{mid_y - 4}" text-anchor="middle" '
                f'font-size="9.5" fill="#64748B" '
                f'style="paint-order:stroke;stroke:#F8FAFC;stroke-width:3px">'
                f'{label}</text>'
            )

    # ── Nodes ────────────────────────────────────────────────────────────────
    for (nid, nx, ny, nw, nh, label, sublabel, color, shape) in nodes:
        is_dash = shape == "rect_dash"
        dash_attr = "stroke-dasharray='6,3'" if is_dash else ""
        # Shadow
        parts.append(
            f'<rect x="{nx+2}" y="{ny+2}" width="{nw}" height="{nh}" '
            f'rx="8" fill="#00000015"/>'
        )
        # Box
        parts.append(
            f'<rect x="{nx}" y="{ny}" width="{nw}" height="{nh}" '
            f'rx="8" fill="{color}18" stroke="{color}" stroke-width="1.8" {dash_attr}/>'
        )
        # Main label
        parts.append(
            f'<text x="{nx + nw//2}" y="{ny + nh//2 - 5}" '
            f'text-anchor="middle" font-size="12" font-weight="700" fill="{color}">'
            f'{label}</text>'
        )
        # Sub label
        parts.append(
            f'<text x="{nx + nw//2}" y="{ny + nh//2 + 10}" '
            f'text-anchor="middle" font-size="9.5" fill="#64748B">'
            f'{sublabel}</text>'
        )

    # ── Legend ────────────────────────────────────────────────────────────────
    legend = [
        ("#6366F1", "HTTP / Transport"),
        ("#0EA5E9", "Adapter layer"),
        ("#10B981", "Canonical model"),
        ("#F59E0B", "Solver / KPIs"),
    ]
    lx = 20
    for color, label in legend:
        parts.append(f'<rect x="{lx}" y="{H-24}" width="12" height="12" rx="3" fill="{color}"/>')
        parts.append(f'<text x="{lx+16}" y="{H-14}" font-size="10" fill="#475569">{label}</text>')
        lx += 130

    parts.append('</svg>')
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Gantt Chart SVG
# ─────────────────────────────────────────────────────────────────────────────

def build_gantt(output: dict, input_data: Optional[dict] = None) -> str:
    assignments = output.get("assignments", [])
    if not assignments:
        return "<p style='color:#64748B'>No assignments to display.</p>"

    all_starts  = [_dt(a["start"]) for a in assignments]
    all_ends    = [_dt(a["end"])   for a in assignments]
    origin      = min(all_starts)
    horizon     = max(all_ends)
    total_min   = _min_from(origin, horizon)

    resources   = sorted(set(a["resource"] for a in assignments))

    prod_family: Dict[str, str] = {}
    prod_due:    Dict[str, datetime] = {}
    if input_data:
        for p in input_data.get("products", []):
            prod_family[p["id"]] = p["family"]
            prod_due[p["id"]]    = _dt(p["due"])
    else:
        for a in assignments:
            prod_family.setdefault(a["product"], "unknown")

    # Changeover gaps
    co_bars = []
    by_res: Dict[str, list] = {}
    for a in assignments:
        by_res.setdefault(a["resource"], []).append(a)
    for res_id, ops in by_res.items():
        for i, cur in enumerate(sorted(ops, key=lambda x: _dt(x["start"]))[:-1]):
            nxt = sorted(ops, key=lambda x: _dt(x["start"]))[i+1]
            gap = (_dt(nxt["start"]) - _dt(cur["end"])).total_seconds() / 60
            if gap > 0:
                co_bars.append({
                    "resource": res_id,
                    "start_m": _min_from(origin, _dt(cur["end"])),
                    "end_m":   _min_from(origin, _dt(nxt["start"])),
                    "gap_min": round(gap),
                })

    ROW_H    = 56
    LABEL_W  = 90
    CHART_W  = 860
    PAD      = 24
    HEADER_H = 44
    chart_h  = HEADER_H + len(resources) * ROW_H + PAD

    COLORS = {"standard": "#4F86C6", "premium": "#E8963A", "unknown": "#94A3B8"}
    CO_COL = "#EF4444"
    GRID   = "#E2E8F0"

    def xp(m): return LABEL_W + (m / total_min) * CHART_W
    def wp(m): return max(2.0, (m / total_min) * CHART_W)

    parts = []
    parts.append(
        f'<svg viewBox="0 0 {LABEL_W+CHART_W+PAD} {chart_h}" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        f'background:#F8FAFC;border-radius:12px;width:100%">'
    )

    # Row backgrounds
    for i, res in enumerate(resources):
        y    = HEADER_H + i * ROW_H
        fill = "#F1F5F9" if i % 2 == 0 else "#F8FAFC"
        parts.append(f'<rect x="{LABEL_W}" y="{y}" width="{CHART_W}" height="{ROW_H}" fill="{fill}"/>')

    # Grid lines + tick labels
    tick = 0
    while tick <= total_min + 1:
        gx = xp(tick)
        parts.append(
            f'<line x1="{gx:.1f}" y1="{HEADER_H}" x2="{gx:.1f}" '
            f'y2="{chart_h-PAD//2}" stroke="{GRID}" stroke-width="1"/>'
        )
        label = (origin + __import__("datetime").timedelta(minutes=tick)).strftime("%H:%M")
        parts.append(
            f'<text x="{gx:.1f}" y="{HEADER_H-8}" text-anchor="middle" '
            f'font-size="11" fill="#94A3B8">{label}</text>'
        )
        tick += 30

    # Resource labels
    for i, res in enumerate(resources):
        cy = HEADER_H + i * ROW_H + ROW_H // 2
        parts.append(
            f'<text x="{LABEL_W-8}" y="{cy+4}" text-anchor="end" '
            f'font-size="12" font-weight="600" fill="#374151">{res}</text>'
        )

    # Changeover bars
    for co in co_bars:
        i  = resources.index(co["resource"])
        y  = HEADER_H + i * ROW_H + 10
        bx = xp(co["start_m"])
        bw = wp(co["end_m"] - co["start_m"])
        bh = ROW_H - 20
        parts.append(
            f'<rect x="{bx:.1f}" y="{y}" width="{bw:.1f}" height="{bh}" '
            f'fill="{CO_COL}" opacity="0.18" rx="3"/>'
        )
        if bw > 22:
            parts.append(
                f'<text x="{bx+bw/2:.1f}" y="{y+bh/2+4:.1f}" text-anchor="middle" '
                f'font-size="9" fill="{CO_COL}" font-weight="600">CO {co["gap_min"]}m</text>'
            )

    # Assignment bars
    for a in assignments:
        i       = resources.index(a["resource"])
        y       = HEADER_H + i * ROW_H + 7
        bh      = ROW_H - 14
        start_m = _min_from(origin, _dt(a["start"]))
        end_m   = _min_from(origin, _dt(a["end"]))
        bx      = xp(start_m)
        bw      = wp(end_m - start_m)
        color   = COLORS.get(prod_family.get(a["product"], "unknown"), "#94A3B8")
        dur_m   = round(end_m - start_m)

        parts.append(f'<rect x="{bx+1:.1f}" y="{y+1}" width="{bw:.1f}" height="{bh}" rx="6" fill="#00000018"/>')
        parts.append(f'<rect x="{bx:.1f}" y="{y}" width="{bw:.1f}" height="{bh}" rx="6" fill="{color}"/>')

        if bw > 55:
            parts.append(
                f'<text x="{bx+bw/2:.1f}" y="{y+bh/2-5:.1f}" text-anchor="middle" '
                f'font-size="10" font-weight="700" fill="white">{a["product"]} s{a["step_index"]}</text>'
            )
            parts.append(
                f'<text x="{bx+bw/2:.1f}" y="{y+bh/2+9:.1f}" text-anchor="middle" '
                f'font-size="9" fill="white" opacity="0.88">{a["capability"]} · {dur_m}m</text>'
            )
        elif bw > 28:
            parts.append(
                f'<text x="{bx+bw/2:.1f}" y="{y+bh/2+4:.1f}" text-anchor="middle" '
                f'font-size="9" font-weight="700" fill="white">{a["product"]}</text>'
            )

    # Due date markers
    for pid, due_dt in prod_due.items():
        if origin <= due_dt <= horizon:
            dm = _min_from(origin, due_dt)
            dx = xp(dm)
            parts.append(
                f'<line x1="{dx:.1f}" y1="{HEADER_H}" x2="{dx:.1f}" '
                f'y2="{chart_h-PAD//2}" stroke="#10B981" stroke-width="1.5" '
                f'stroke-dasharray="4,3" opacity="0.75"/>'
            )
            parts.append(
                f'<text x="{dx+3:.1f}" y="{HEADER_H+13}" font-size="9" '
                f'fill="#10B981" font-weight="600">{pid}</text>'
            )

    parts.append('</svg>')
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Full HTML page
# ─────────────────────────────────────────────────────────────────────────────

def generate_html(output: dict, input_data: Optional[dict] = None) -> str:
    kpis        = output.get("kpis", {})
    flow_svg    = build_flow_diagram()
    gantt_svg   = build_gantt(output, input_data)

    util_html = "".join(
        f'<div class="util-row">'
        f'<span class="util-label">{res}</span>'
        f'<div class="util-track"><div class="util-fill" style="width:{pct}%"></div></div>'
        f'<span class="util-pct">{pct}%</span>'
        f'</div>'
        for res, pct in kpis.get("utilization_pct", {}).items()
    )

    gantt_legend = """
        <div class="legend">
          <span class="leg"><span class="dot" style="background:#4F86C6"></span>Standard family</span>
          <span class="leg"><span class="dot" style="background:#E8963A"></span>Premium family</span>
          <span class="leg"><span class="dot" style="background:#EF4444;opacity:.4"></span>Changeover gap</span>
          <span class="leg"><span class="dot" style="background:#10B981"></span>Due date</span>
        </div>"""

    tard    = kpis.get("tardiness_minutes", "—")
    tard_cls = "green" if tard == 0 else "red"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Harmony Scheduler — Visualization</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0 }}
body {{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#F1F5F9; color:#1E293B; padding:32px;
}}
h1 {{ font-size:22px; font-weight:700; color:#0F172A; margin-bottom:4px }}
.subtitle {{ font-size:13px; color:#64748B; margin-bottom:28px }}

.card {{
  background:white; border-radius:14px;
  box-shadow:0 1px 4px rgba(0,0,0,.07);
  padding:24px; margin-bottom:22px;
}}
.card-title {{
  font-size:11px; font-weight:700; color:#94A3B8;
  text-transform:uppercase; letter-spacing:.08em; margin-bottom:18px;
}}

/* KPI grid */
.kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:14px }}
.kpi {{ background:#F8FAFC; border-radius:10px; padding:18px; text-align:center }}
.kpi-val {{ font-size:30px; font-weight:800; line-height:1; margin-bottom:5px }}
.kpi-lbl {{ font-size:11px; color:#64748B; font-weight:500 }}
.green {{ color:#10B981 }}
.red   {{ color:#EF4444 }}
.blue  {{ color:#4F86C6 }}
.amber {{ color:#F59E0B }}

/* Utilization */
.util-row {{ display:flex; align-items:center; gap:10px; margin-bottom:10px }}
.util-label {{ font-size:12px; font-weight:600; color:#374151; width:72px; flex-shrink:0 }}
.util-track {{ flex:1; background:#F1F5F9; border-radius:4px; height:8px; overflow:hidden }}
.util-fill  {{ height:100%; background:#4F86C6; border-radius:4px }}
.util-pct   {{ font-size:12px; font-weight:600; color:#374151; width:36px; text-align:right }}

/* Legend */
.legend {{ display:flex; flex-wrap:wrap; gap:14px; margin-top:16px;
           padding-top:14px; border-top:1px solid #F1F5F9 }}
.leg    {{ display:flex; align-items:center; gap:6px; font-size:12px; color:#475569 }}
.dot    {{ width:12px; height:12px; border-radius:3px; display:inline-block; flex-shrink:0 }}

/* Tabs */
.tabs {{ display:flex; gap:8px; margin-bottom:16px }}
.tab {{
  padding:7px 18px; border-radius:8px; font-size:13px; font-weight:600;
  cursor:pointer; border:none; background:#F1F5F9; color:#64748B;
}}
.tab.active {{ background:#4F86C6; color:white }}
.tab-panel {{ display:none }}
.tab-panel.active {{ display:block }}
</style>
</head>
<body>

<h1>Harmony Production Schedule</h1>
<p class="subtitle">
  Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} &nbsp;·&nbsp;
  Solver status: <strong>{output.get("solver_status", "—")}</strong>
</p>

<!-- KPIs -->
<div class="card">
  <div class="card-title">KPI Summary</div>
  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-val {tard_cls}">{tard}</div>
      <div class="kpi-lbl">Tardiness (min)</div>
    </div>
    <div class="kpi">
      <div class="kpi-val blue">{kpis.get("makespan_minutes","—")}</div>
      <div class="kpi-lbl">Makespan (min)</div>
    </div>
    <div class="kpi">
      <div class="kpi-val amber">{kpis.get("changeover_count","—")}</div>
      <div class="kpi-lbl">Changeovers</div>
    </div>
    <div class="kpi">
      <div class="kpi-val amber">{kpis.get("changeover_minutes","—")}</div>
      <div class="kpi-lbl">Changeover (min)</div>
    </div>
  </div>
</div>

<!-- Tabs: Gantt + Flow -->
<div class="card">
  <div class="tabs">
    <button class="tab active" onclick="showTab('gantt',this)">📅 Gantt Chart</button>
    <button class="tab"        onclick="showTab('flow',this)">🔀 System Flow</button>
  </div>

  <div id="tab-gantt" class="tab-panel active">
    <div class="card-title">Production Schedule</div>
    {gantt_svg}
    {gantt_legend}
  </div>

  <div id="tab-flow" class="tab-panel">
    <div class="card-title">Request Flow Through the System</div>
    {flow_svg}
  </div>
</div>

<!-- Utilization -->
<div class="card">
  <div class="card-title">Resource Utilization</div>
  {util_html}
</div>

<script>
function showTab(name, btn) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
}}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python visualize.py output.json [input.json]")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        output = json.load(f)

    input_data = None
    if len(sys.argv) >= 3:
        with open(sys.argv[2]) as f:
            input_data = json.load(f)

    html     = generate_html(output, input_data)
    out_path = "schedule_viz.html"

    with open(out_path, "w") as f:
        f.write(html)

    print(f"Saved → {out_path}")
    webbrowser.open(f"file://{os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
