"""Build a self-contained HTML results report from result.json files.

Charts are inline SVG generated here; the page has no external dependencies. Colors follow a
validated categorical palette (light and dark steps chosen separately), and every value shown
in a chart is also in a table, so nothing depends on hover or on color alone.
"""

import html
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

TIERS = ["easy", "medium", "hard"]
# Categorical slots, fixed order (light, dark). Validated adjacent-pair CVD-safe in both modes;
# the first three are also safe all-pairs, which is what the scatter uses.
SERIES = [
    ("#2a78d6", "#3987e5"),
    ("#eb6834", "#d95926"),
    ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"),
    ("#008300", "#008300"),
    ("#6250d6", "#9085e9"),
    ("#e34948", "#e66767"),
]
# Single-hue sequential ramp (blue) for the matrix, by solve-rate bucket
RAMP = ["#cde2fb", "#9ec5f4", "#5598e7", "#256abf", "#104281"]
# Charts are drawn at the card's real width so 11-12px text renders at 1:1
W, H = 960, 320
PAD_L, PAD_R, PAD_T, PAD_B = 48, 140, 16, 40


# --- data ---------------------------------------------------------------------------------


@dataclass
class Row:
    label: str
    model: str
    fault: str
    tier: str
    resolved: bool
    solved: bool  # resolved and the root cause judged correct
    declared: bool
    rc: str
    fix_quality: float
    fix_label: str
    steps: int
    max_steps: int
    cost: float | None
    ttr: float | None
    fallback: int
    path: str


def label_for(path: Path, r: dict) -> str:
    parts = path.parts
    if "sweeps" in parts:
        return parts[-4]  # runs/sweeps/<name>/<label>/<fault>/r<n>/result.json
    effort = f"-{r['effort']}" if r.get("effort") else ""
    steps = f"-{r['max_steps']}s" if r.get("max_steps") and r["max_steps"] != 20 else ""
    return f"{r['model']}{effort}{steps}"


def load_rows(root: Path) -> list[Row]:
    rows = []
    for f in sorted(root.glob("**/result.json")):
        r = json.loads(f.read_text())
        if not r.get("steps") and r.get("stop_reason") == "refusal":
            continue
        cost = r.get("agent_cost_usd")
        if cost is not None:
            cost += r.get("judge_cost_usd") or 0
        rows.append(
            Row(
                label=label_for(f, r),
                model=r["model"],
                fault=r["fault_id"],
                tier=r["tier"],
                resolved=r["resolved"],
                solved=r["resolved"] and r.get("root_cause_verdict") == "correct",
                declared=r["declared"],
                rc=r.get("root_cause_verdict") or "-",
                fix_quality=r.get("fix_quality", 0.0),
                fix_label=r.get("fix_label", "-"),
                steps=r["steps"],
                max_steps=r.get("max_steps") or 20,
                cost=cost,
                ttr=r.get("time_to_resolve_seconds"),
                fallback=r.get("fallback_turns", 0),
                path=str(f.parent),
            )
        )
    return rows


def rate(rows: list[Row], attr: str) -> float:
    return sum(getattr(r, attr) for r in rows) / len(rows) if rows else 0.0


def mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


# --- helpers ------------------------------------------------------------------------------


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def tip(*lines: str) -> str:
    """A tooltip payload: first line is the value (strong), the rest are context."""
    return esc(json.dumps(lines))


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def money(x: float | None) -> str:
    return "-" if x is None else f"${x:.3f}"


def nice_max(x: float) -> float:
    if x <= 0:
        return 1.0
    for step in (0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10):
        top = step * 5
        if x <= top:
            return top
    return x * 1.2


def y_axis(ticks: list[tuple[float, str]], y0: float, y1: float, vmax: float) -> str:
    out = []
    for v, text in ticks:
        y = y0 - (v / vmax) * (y0 - y1)
        cls = "base" if v == 0 else "grid"
        out.append(f'<line class="{cls}" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(
            f'<text class="tick" x="{PAD_L - 8}" y="{y + 4:.1f}" text-anchor="end">{text}</text>'
        )
    return "".join(out)


def svg(body: str, title: str, height: int = H) -> str:
    return (
        f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="{esc(title)}">'
        f"<title>{esc(title)}</title>{body}</svg>"
    )


def legend(items: list[tuple[str, int]], shape: str = "rect") -> str:
    keys = []
    for name, slot in items:
        mark = f'<span class="key key-{shape}" style="--c: var(--s{slot})"></span>'
        keys.append(f'<span class="legend-item">{mark}{esc(name)}</span>')
    return f'<div class="legend">{"".join(keys)}</div>'


# --- charts -------------------------------------------------------------------------------


def scatter(configs: dict[str, list[Row]], model_slot: dict[str, int]) -> str:
    """Solve rate vs mean cost per episode; one point per configuration, colored by model."""
    pts = []
    for label, rows in configs.items():
        c = mean(r.cost for r in rows)
        if c is not None:
            pts.append((label, rows[0].model, c, rate(rows, "solved"), len(rows)))
    if not pts:
        return "<p class='muted'>No cost data yet.</p>"
    xmax = nice_max(max(p[2] for p in pts))
    y0, y1 = H - PAD_B, PAD_T
    x0, x1 = PAD_L, W - PAD_R
    body = [y_axis([(v / 100, f"{v}%") for v in (0, 25, 50, 75, 100)], y0, y1, 1.0)]
    for i in range(6):
        v = xmax * i / 5
        x = x0 + (v / xmax) * (x1 - x0)
        body.append(
            f'<text class="tick" x="{x:.1f}" y="{y0 + 18}" text-anchor="middle">${v:.3f}</text>'
        )
    body.append(
        f'<text class="axis-title" x="{(x0 + x1) / 2}" y="{H - 2}" text-anchor="middle">mean cost per episode</text>'
    )
    for label, model, c, s, n in sorted(pts, key=lambda p: p[2]):
        x = x0 + (c / xmax) * (x1 - x0)
        y = y0 - s * (y0 - y1)
        slot = model_slot[model]
        t = tip(f"{pct(s)} solved", label, f"{money(c)} per episode", f"{n} episodes")
        body.append(
            f'<g class="pt" tabindex="0" data-tip="{t}">'
            f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="12"/>'
            f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="5" style="fill: var(--s{slot})"/>'
            f'<text class="label" x="{x + 10:.1f}" y="{y + 4:.1f}">{esc(label)}</text></g>'
        )
    return svg("".join(body), "Solve rate against mean cost per episode, by configuration")


def tier_bars(configs: dict[str, list[Row]], slots: dict[str, int]) -> str:
    """Grouped columns: solve rate per tier, one column per configuration."""
    labels = list(configs)
    y0, y1 = H - PAD_B, PAD_T
    x0, x1 = PAD_L, W - 24
    band = (x1 - x0) / len(TIERS)
    bar = min(24, (band * 0.8 - 2 * (len(labels) - 1)) / max(1, len(labels)))
    group = len(labels) * bar + 2 * (len(labels) - 1)
    body = [y_axis([(v / 100, f"{v}%") for v in (0, 25, 50, 75, 100)], y0, y1, 1.0)]
    for t_i, tier in enumerate(TIERS):
        start = x0 + band * t_i + (band - group) / 2
        body.append(
            f'<text class="tick" x="{x0 + band * (t_i + 0.5):.1f}" y="{y0 + 18}" '
            f'text-anchor="middle">{tier}</text>'
        )
        for c_i, label in enumerate(labels):
            rows = [r for r in configs[label] if r.tier == tier]
            if not rows:
                continue
            s = rate(rows, "solved")
            # A 2px stub on the baseline keeps a 0% column visible and hoverable
            h = max(2.0, s * (y0 - y1))
            x = start + c_i * (bar + 2)
            t = tip(f"{pct(s)} solved", f"{label}, {tier}", f"{len(rows)} episodes")
            # 4px rounded data end, square at the baseline
            r_ = min(4, h / 2, bar / 2)
            d = (
                f"M{x:.1f},{y0} V{y0 - h + r_:.1f} Q{x:.1f},{y0 - h:.1f} {x + r_:.1f},{y0 - h:.1f} "
                f"H{x + bar - r_:.1f} Q{x + bar:.1f},{y0 - h:.1f} {x + bar:.1f},{y0 - h + r_:.1f} "
                f"V{y0} Z"
            )
            body.append(
                f'<g class="bar" tabindex="0" data-tip="{t}">'
                f'<rect class="hit" x="{x - 1:.1f}" y="{y1}" width="{bar + 2:.1f}" height="{y0 - y1}"/>'
                f'<path d="{d}" style="fill: var(--s{slots[label]})"/></g>'
            )
    return svg("".join(body), "Solve rate by tier and configuration")


def step_curve(configs: dict[str, list[Row]], model_slot: dict[str, int]) -> str | None:
    """Solve rate against the step budget, one line per model. None if steps never vary."""
    by_model: dict[str, dict[int, list[Row]]] = defaultdict(lambda: defaultdict(list))
    for rows in configs.values():
        for r in rows:
            by_model[r.model][r.max_steps].append(r)
    by_model = {m: s for m, s in by_model.items() if len(s) >= 2}
    if not by_model:
        return None
    all_steps = sorted({s for series in by_model.values() for s in series})
    xmin, xmax = min(all_steps), max(all_steps)
    y0, y1 = H - PAD_B, PAD_T
    x0, x1 = PAD_L, W - PAD_R

    def xs(v):
        return x0 + (v - xmin) / (xmax - xmin) * (x1 - x0) if xmax > xmin else (x0 + x1) / 2

    body = [y_axis([(v / 100, f"{v}%") for v in (0, 25, 50, 75, 100)], y0, y1, 1.0)]
    for s in all_steps:
        body.append(
            f'<text class="tick" x="{xs(s):.1f}" y="{y0 + 18}" text-anchor="middle">{s}</text>'
        )
    body.append(
        f'<text class="axis-title" x="{(x0 + x1) / 2}" y="{H - 2}" text-anchor="middle">step budget (tool calls)</text>'
    )
    readout: dict[int, list[str]] = defaultdict(list)
    for model, series in by_model.items():
        slot = model_slot[model]
        pts = [(s, rate(rows, "solved"), len(rows)) for s, rows in sorted(series.items())]
        path = " ".join(
            f"{'M' if i == 0 else 'L'}{xs(s):.1f},{y0 - v * (y0 - y1):.1f}"
            for i, (s, v, _) in enumerate(pts)
        )
        body.append(f'<path class="line" d="{path}" style="stroke: var(--s{slot})"/>')
        for s, v, n in pts:
            body.append(
                f'<circle class="dot" cx="{xs(s):.1f}" cy="{y0 - v * (y0 - y1):.1f}" r="4" style="fill: var(--s{slot})"/>'
            )
            readout[s].append(f"{pct(v)}  {model} ({n})")
        s_last, v_last, _ = pts[-1]
        body.append(
            f'<text class="label" x="{xs(s_last) + 10:.1f}" y="{y0 - v_last * (y0 - y1) + 4:.1f}">{esc(model)}</text>'
        )
    # Crosshair columns: the whole column is the hit target, snapping to each budget
    for i, s in enumerate(all_steps):
        left = (xs(all_steps[i - 1]) + xs(s)) / 2 if i else x0
        right = (xs(s) + xs(all_steps[i + 1])) / 2 if i < len(all_steps) - 1 else x1
        t = tip(*readout[s], f"{s}-call budget")
        body.append(
            f'<g class="col" tabindex="0" data-tip="{t}" data-x="{xs(s):.1f}">'
            f'<rect class="hit" x="{left:.1f}" y="{y1}" width="{right - left:.1f}" height="{y0 - y1}"/>'
            f'<line class="crosshair" x1="{xs(s):.1f}" x2="{xs(s):.1f}" y1="{y1}" y2="{y0}"/></g>'
        )
    return svg("".join(body), "Solve rate against step budget, by model")


def matrix(configs: dict[str, list[Row]], faults: list[tuple[str, str]]) -> str:
    """Fault x configuration grid; each cell shows solved/total on a one-hue ramp."""
    head = "".join(f"<th>{esc(label)}</th>" for label in configs)
    body = []
    for fault, tier in faults:
        cells = []
        for label, rows in configs.items():
            mine = [r for r in rows if r.fault == fault]
            if not mine:
                cells.append('<td class="empty">-</td>')
                continue
            solved = sum(r.solved for r in mine)
            s = solved / len(mine)
            step = min(len(RAMP) - 1, round(s * (len(RAMP) - 1)))
            ink = "#ffffff" if step >= 3 else "#0b0b0b"
            detail = [
                f"{solved}/{len(mine)} solved",
                f"{label} on {fault}",
                f"resolved {sum(r.resolved for r in mine)}/{len(mine)}, "
                f"declared {sum(r.declared for r in mine)}/{len(mine)}",
                "fix: " + ", ".join(sorted({r.fix_label for r in mine})),
            ]
            cells.append(
                f'<td class="cell" tabindex="0" data-tip="{tip(*detail)}" '
                f'style="background: {RAMP[step]}; color: {ink}">{solved}/{len(mine)}</td>'
            )
        body.append(
            f"<tr><th class='row'>{esc(fault)} <span class='muted'>{tier}</span></th>{''.join(cells)}</tr>"
        )
    return f'<table class="matrix"><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


# --- page ---------------------------------------------------------------------------------


def summary_table(configs: dict[str, list[Row]]) -> str:
    head = [
        "Configuration",
        "Model",
        "Episodes",
        "Solved",
        "Resolved",
        "Declared",
        "Root cause correct",
        "Mean fix quality",
        "Mean steps",
        "Mean cost",
        "Total cost",
        "Fallback runs",
    ]
    rows = []
    for label, rs in configs.items():
        costs = [r.cost for r in rs if r.cost is not None]
        total = sum(costs) if costs else None
        rows.append(
            [
                label,
                rs[0].model,
                len(rs),
                pct(rate(rs, "solved")),
                pct(rate(rs, "resolved")),
                pct(rate(rs, "declared")),
                pct(sum(r.rc == "correct" for r in rs) / len(rs)),
                f"{mean(r.fix_quality for r in rs):.2f}",
                f"{mean(r.steps for r in rs):.1f}",
                money(mean(r.cost for r in rs)),
                money(total),
                sum(r.fallback > 0 for r in rs),
            ]
        )
    th = "".join(f"<th>{h}</th>" for h in head)
    trs = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f'<table class="data"><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>'


def episode_table(rows: list[Row]) -> str:
    head = [
        "Configuration",
        "Fault",
        "Tier",
        "Solved",
        "Resolved",
        "Root cause",
        "Fix",
        "Steps",
        "Cost",
        "Time to resolve",
    ]
    trs = []
    for r in rows:
        cells = [
            r.label,
            r.fault,
            r.tier,
            "yes" if r.solved else "no",
            "yes" if r.resolved else "no",
            r.rc,
            f"{r.fix_label} {r.fix_quality:.2f}",
            f"{r.steps}/{r.max_steps}",
            money(r.cost),
            f"{r.ttr:.0f}s" if r.ttr else "-",
        ]
        trs.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in cells) + "</tr>")
    th = "".join(f"<th>{h}</th>" for h in head)
    return f'<table class="data"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>'


def series_css() -> str:
    light = "".join(f"--s{i}: {lt};" for i, (lt, _) in enumerate(SERIES))
    dark = "".join(f"--s{i}: {dk};" for i, (_, dk) in enumerate(SERIES))
    return light, dark


CSS = """
.viz-root { color-scheme: light; --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b;
  --ink-2: #52514e; --muted: #898781; --grid: #e1e0d9; --base: #c3c2b7;
  --ring: rgba(11,11,11,0.10); __LIGHT__ }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) .viz-root {
  color-scheme: dark; --surface: #1a1a19; --page: #0d0d0d; --ink: #f0efec; --ink-2: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --base: #383835; --ring: rgba(255,255,255,0.10); __DARK__ } }
:root[data-theme="dark"] .viz-root { color-scheme: dark; --surface: #1a1a19; --page: #0d0d0d;
  --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781; --grid: #2c2c2a; --base: #383835;
  --ring: rgba(255,255,255,0.10); __DARK__ }
html, body { margin: 0; }
body.viz-root { background: var(--page); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 0 0 4px; }
.muted { color: var(--muted); } .sub { color: var(--ink-2); margin: 0 0 12px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px;
  padding: 16px; margin: 16px 0; overflow-x: auto; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
.grid2 .card { margin: 0; }
svg { width: 100%; height: auto; display: block; overflow: visible; }
svg .grid { stroke: var(--grid); stroke-width: 1; } svg .base { stroke: var(--base); stroke-width: 1; }
svg .tick, svg .axis-title { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
svg .label { fill: var(--ink-2); font-size: 12px; }
svg .dot { stroke: var(--surface); stroke-width: 2; }
svg .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
svg .hit { fill: transparent; } svg .crosshair { stroke: var(--base); stroke-width: 1; opacity: 0; }
svg .col:hover .crosshair, svg .col:focus .crosshair { opacity: 1; }
svg .bar:hover path, svg .bar:focus path, svg .pt:hover .dot, svg .pt:focus .dot { filter: brightness(1.15); }
svg g:focus { outline: none; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 4px 0 8px; color: var(--ink-2); font-size: 12px; }
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.key-rect { width: 10px; height: 10px; border-radius: 2px; background: var(--c); }
.key-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--c); }
table { border-collapse: collapse; font-size: 13px; }
table.data { width: 100%; } .data th, .data td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
.data td { font-variant-numeric: tabular-nums; } .data th { color: var(--ink-2); font-weight: 600; white-space: normal; vertical-align: bottom; }
.matrix th { color: var(--ink-2); font-weight: 600; padding: 6px 8px; text-align: center; font-size: 12px; }
.matrix th.row { text-align: left; white-space: nowrap; }
.matrix td { text-align: center; padding: 6px 10px; min-width: 56px; border: 2px solid var(--surface);
  border-radius: 6px; font-variant-numeric: tabular-nums; font-weight: 600; }
.matrix td.empty { color: var(--muted); font-weight: 400; }
.matrix td.cell:hover, .matrix td.cell:focus { outline: 2px solid var(--ink); outline-offset: -2px; }
details summary { cursor: pointer; color: var(--ink-2); }
#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); display: none; max-width: 280px; z-index: 10; }
#tip strong { display: block; font-size: 14px; } #tip span { display: block; color: var(--ink-2); }
"""

JS = """
const tipEl = document.getElementById('tip');
function show(el, x, y) {
  const lines = JSON.parse(el.dataset.tip);
  tipEl.replaceChildren();
  lines.forEach((t, i) => { const n = document.createElement(i ? 'span' : 'strong');
    n.textContent = t; tipEl.appendChild(n); });
  tipEl.style.display = 'block';
  const r = tipEl.getBoundingClientRect();
  tipEl.style.left = Math.min(x + 14, innerWidth - r.width - 8) + 'px';
  tipEl.style.top = Math.max(8, y - r.height - 10) + 'px';
}
document.querySelectorAll('[data-tip]').forEach(el => {
  el.addEventListener('pointermove', e => show(el, e.clientX, e.clientY));
  el.addEventListener('pointerleave', () => tipEl.style.display = 'none');
  el.addEventListener('focus', () => { const b = el.getBoundingClientRect(); show(el, b.right, b.top); });
  el.addEventListener('blur', () => tipEl.style.display = 'none');
});
"""


def build(root: Path, title: str = "kubebreaker results") -> str:
    rows = load_rows(root)
    if not rows:
        raise ValueError(f"no results under {root}")
    configs: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        configs[r.label].append(r)
    configs = dict(sorted(configs.items()))
    slots = {label: i % len(SERIES) for i, label in enumerate(configs)}
    models = sorted({r.model for r in rows})
    model_slot = {m: i for i, m in enumerate(models)}
    faults = sorted({(r.fault, r.tier) for r in rows}, key=lambda f: (TIERS.index(f[1]), f[0]))
    light, dark = series_css()
    curve = step_curve(configs, model_slot)
    total_cost = sum(r.cost for r in rows if r.cost is not None)

    scatter_card = (
        '<div class="card"><h2>Solve rate vs cost</h2>'
        '<p class="sub">One point per configuration. Solved = the app recovered and the root '
        "cause was judged correct.</p>"
        + legend([(m, model_slot[m]) for m in models], "dot")
        + scatter(configs, model_slot)
        + "</div>"
    )
    tier_card = (
        '<div class="card"><h2>Solve rate by tier</h2><p class="sub">Easy, medium and hard faults.</p>'
        + legend([(label, slots[label]) for label in configs])
        + tier_bars(configs, slots)
        + "</div>"
    )
    curve_card = (
        '<div class="card"><h2>Step budget</h2><p class="sub">Solve rate as the tool-call budget grows.</p>'
        + legend([(m, model_slot[m]) for m in models], "dot")
        + curve
        + "</div>"
        if curve
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>{CSS.replace("__LIGHT__", light).replace("__DARK__", dark)}</style></head>
<body class="viz-root"><main>
<h1>{esc(title)}</h1>
<p class="sub">{len(rows)} episodes, {len(configs)} configurations, {len(faults)} faults,
${total_cost:.2f} total. Generated {datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")} from {esc(root)}.</p>
<div class="card"><h2>Configurations</h2>{summary_table(configs)}</div>
{scatter_card}{tier_card}
{curve_card}
<div class="card"><h2>Fault by configuration</h2>
<p class="sub">Solved episodes out of total, darker is better. Hover a cell for details.</p>
{matrix(configs, faults)}</div>
<div class="card"><details><summary>All {len(rows)} episodes</summary>{episode_table(rows)}</details></div>
</main><div id="tip" role="tooltip"></div><script>{JS}</script></body></html>
"""


def write(root: Path, out: Path, title: str = "kubebreaker results") -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(root, title))
    return out
