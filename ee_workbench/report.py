"""Test reports: a sequence run or a recorded session as one HTML page (plots
drawn as inline SVG, so the file stands alone) or a PDF (via WeasyPrint, when
installed: setup-ubuntu.sh --docs, or pip install weasyprint).

    html = sequence_report(title, rows, columns, plan, log, instruments, outcome)
    html = session_report("bench.eewb")
    save(html, "report.pdf")         # .html or .pdf by the suffix
"""
from __future__ import annotations

import html as _html
import math
import platform
import time
from pathlib import Path

import numpy as np

PALETTE = ["#1f77b4", "#d6542b", "#2a9d5c", "#8e5bd6", "#c2185b", "#00838f", "#9e7b00"]
MAX_PLOT_POINTS = 1500
MAX_TABLE_ROWS = 2000

CSS = """
body { font-family: "DejaVu Sans", "Helvetica Neue", Arial, sans-serif; color:#1d2026;
       margin: 28px auto; max-width: 980px; font-size: 13px; line-height: 1.45; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 26px 0 8px; border-bottom: 1px solid #d8dce3; padding-bottom: 4px; }
.sub { color:#5b6270; margin-bottom: 18px; }
table { border-collapse: collapse; width: 100%; font-size: 12px; }
th, td { border: 1px solid #d8dce3; padding: 3px 6px; text-align: left; vertical-align: top; }
th { background: #f1f3f6; }
td.num { text-align: right; font-family: "DejaVu Sans Mono", monospace; }
tr.fail td { background: #fdecea; }
td.fail { background: #f8c9c4 !important; font-weight: bold; }
.badge { display:inline-block; padding: 3px 10px; border-radius: 4px; font-weight: bold;
         color: #fff; }
.pass { background: #2a9d5c; } .failb { background: #c62828; } .other { background:#6b7280; }
.kv td:first-child { width: 180px; color:#5b6270; }
pre { background:#f6f7f9; border:1px solid #e1e4e9; padding: 8px; font-size: 11px;
      white-space: pre-wrap; }
svg { display:block; margin: 6px 0 14px; }
.small { color:#5b6270; font-size: 11px; }
@page { size: A4; margin: 16mm 14mm; }
"""


def esc(x) -> str:
    return _html.escape(str(x))


def fmt(v) -> str:
    if isinstance(v, float):
        if math.isnan(v):
            return "—"
        return f"{v:.6g}"
    return esc(v)


# ---------------------------------------------------------------- SVG plots
def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if not math.isfinite(lo) or not math.isfinite(hi):
        return []
    if hi <= lo:
        hi = lo + (abs(lo) or 1.0)
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=10 * mag)
    start = math.ceil(lo / step) * step
    out = []
    v = start
    while v <= hi + step * 1e-9:
        out.append(round(v, 12))
        v += step
    return out


def _tick_label(v: float) -> str:
    a = abs(v)
    if a and (a < 1e-3 or a >= 1e5):
        return f"{v:.2e}"
    return f"{v:.6g}"


def downsample(x: np.ndarray, y: np.ndarray, n: int = MAX_PLOT_POINTS):
    """Min/max envelope so spikes survive."""
    if x.size <= n:
        return x, y
    k = int(math.ceil(x.size / (n / 2)))
    m = (x.size // k) * k
    xs = x[:m].reshape(-1, k)
    ys = y[:m].reshape(-1, k)
    with np.errstate(all="ignore"):
        lo = np.nanmin(ys, 1)
        hi = np.nanmax(ys, 1)
    xx = np.repeat(xs.mean(1), 2)
    yy = np.empty(xx.size)
    yy[0::2], yy[1::2] = lo, hi
    return xx, yy


def svg_plot(series: list[tuple[str, np.ndarray, np.ndarray]], xlabel: str = "",
             ylabel: str = "", width: int = 900, height: int = 260, markers: bool = False,
             vlines: list[tuple[float, str]] | None = None) -> str:
    """series: (label, x, y). Returns an <svg> element."""
    ml, mr, mt, mb = 70, 16, 14, 40
    pw, ph = width - ml - mr, height - mt - mb
    pts = []
    for lab, x, y in series:
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        ok = np.isfinite(x) & np.isfinite(y)
        x, y = downsample(x[ok], y[ok])
        pts.append((lab, x, y))
    allx = np.concatenate([p[1] for p in pts]) if pts else np.array([])
    ally = np.concatenate([p[2] for p in pts]) if pts else np.array([])
    if not allx.size:
        return f'<svg width="{width}" height="40"><text x="10" y="24" ' \
               f'font-size="12" fill="#888">no data</text></svg>'
    x0, x1 = float(allx.min()), float(allx.max())
    y0, y1 = float(ally.min()), float(ally.max())
    if x1 <= x0:
        x0, x1 = x0 - 0.5, x1 + 0.5
    if y1 <= y0:
        pad = abs(y0) * 0.05 or 0.5
        y0, y1 = y0 - pad, y1 + pad
    else:
        pad = (y1 - y0) * 0.06
        y0, y1 = y0 - pad, y1 + pad

    def sx(v):
        return ml + (v - x0) / (x1 - x0) * pw

    def sy(v):
        return mt + ph - (v - y0) / (y1 - y0) * ph

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="DejaVu Sans, Arial" font-size="10">',
           f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="#fff" stroke="#c9ced6"/>']
    for v in _nice_ticks(y0, y1):
        y = sy(v)
        out.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}" stroke="#eceef2"/>')
        out.append(f'<text x="{ml - 6}" y="{y + 3:.1f}" text-anchor="end" fill="#555">'
                   f'{_tick_label(v)}</text>')
    for v in _nice_ticks(x0, x1, 8):
        x = sx(v)
        out.append(f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + ph}" stroke="#eceef2"/>')
        out.append(f'<text x="{x:.1f}" y="{mt + ph + 14}" text-anchor="middle" fill="#555">'
                   f'{_tick_label(v)}</text>')
    for t, text in vlines or []:
        if x0 <= t <= x1:
            x = sx(t)
            out.append(f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + ph}" '
                       f'stroke="#c99a00" stroke-dasharray="4 3"/>')
            out.append(f'<text x="{x + 3:.1f}" y="{mt + 10}" fill="#9e7b00">{esc(text[:30])}</text>')
    for k, (lab, x, y) in enumerate(pts):
        col = PALETTE[k % len(PALETTE)]
        if x.size:
            d = " ".join(f"{sx(a):.1f},{sy(b):.1f}" for a, b in zip(x, y))
            out.append(f'<polyline fill="none" stroke="{col}" stroke-width="1.4" points="{d}"/>')
            if markers and x.size <= 200:
                out += [f'<circle cx="{sx(a):.1f}" cy="{sy(b):.1f}" r="2.6" fill="{col}"/>'
                        for a, b in zip(x, y)]
        if len(pts) > 1:
            out.append(f'<text x="{ml + 8 + 150 * k}" y="{mt + ph + 32}" fill="{col}">'
                       f'■ {esc(lab)}</text>')
    out.append(f'<text x="{ml + pw / 2}" y="{height - 4}" text-anchor="middle" '
               f'fill="#333">{esc(xlabel)}</text>')
    out.append(f'<text x="12" y="{mt + ph / 2}" text-anchor="middle" fill="#333" '
               f'transform="rotate(-90 12 {mt + ph / 2})">{esc(ylabel)}</text>')
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------- pages
def _page(title: str, body: list[str]) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title>"
            f"<style>{CSS}</style></head><body>" + "\n".join(body) + "</body></html>")


def _table(columns: list[str], rows: list[dict], fail_cols=None) -> str:
    out = ["<table><tr>" + "".join(f"<th>{esc(c)}</th>" for c in columns) + "</tr>"]
    for r in rows[:MAX_TABLE_ROWS]:
        failed = r.get("result") == "FAIL"
        bad = set(fail_cols(r)) if fail_cols else set()
        cells = []
        for c in columns:
            v = r.get(c, "")
            cls = ["num"] if isinstance(v, (int, float)) and not isinstance(v, bool) else []
            if c in bad or (c == "result" and failed):
                cls.append("fail")
            cells.append(f"<td class='{' '.join(cls)}'>{fmt(v)}</td>")
        out.append(f"<tr class='{'fail' if failed else ''}'>" + "".join(cells) + "</tr>")
    out.append("</table>")
    if len(rows) > MAX_TABLE_ROWS:
        out.append(f"<p class='small'>… and {len(rows) - MAX_TABLE_ROWS} more rows "
                   "(save the CSV for all of them).</p>")
    return "\n".join(out)


def failing_columns(row: dict) -> list[str]:
    """Column names named in a row's 'failures' text ("col=value (want …); …")."""
    out = []
    for part in str(row.get("failures", "")).split("; "):
        if "=" in part:
            out.append(part.split("=", 1)[0])
    return out


def sequence_report(title: str, rows: list[dict], columns: list[str], plan: dict | None,
                    log: str, instruments: list[tuple[str, str]], outcome: str,
                    started: float | None = None, x_col: str = "", y_cols=None) -> str:
    started = started or time.time()
    passed = sum(1 for r in rows if r.get("result") == "PASS")
    failed = sum(1 for r in rows if r.get("result") == "FAIL")
    if failed:
        badge = f"<span class='badge failb'>FAIL</span> {failed} of {passed + failed} failed"
    elif passed:
        badge = f"<span class='badge pass'>PASS</span> all {passed} passed"
    else:
        badge = "<span class='badge other'>NO LIMITS</span> results only"
    body = [f"<h1>{esc(title)}</h1>",
            f"<div class='sub'>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}"
            f" · {esc(platform.node())} · EE Workbench</div>",
            f"<p>{badge} &nbsp; <b>{esc(outcome)}</b> · {len(rows)} rows</p>",
            "<h2>Instruments</h2><table class='kv'>"]
    body += [f"<tr><td>{esc(n)}</td><td>{esc(i)}</td></tr>" for n, i in instruments]
    body.append("</table>")
    numeric = [c for c in columns if c not in ("t", "pass", "x", "result", "failures")
               and any(isinstance(r.get(c), (int, float)) for r in rows)]
    xc = x_col or ("x" if "x" in columns else "pass")
    ys = y_cols or numeric[:4]
    if rows and ys:
        body.append("<h2>Plots</h2>")
        for y in ys:
            pts = [(r.get(xc), r.get(y)) for r in rows]
            pts = [(a, b) for a, b in pts if isinstance(a, (int, float))
                   and isinstance(b, (int, float))]
            if pts:
                a = np.array(pts, float)
                body.append(svg_plot([(y, a[:, 0], a[:, 1])], xc, y, height=220, markers=True))
    body.append("<h2>Results</h2>")
    body.append(_table(columns, rows, failing_columns) if rows else "<p>No rows.</p>")
    if plan:
        body.append("<h2>Sequence</h2>")
        steps = plan.get("steps", [])
        if steps:
            cols = ["action", "target", "what", "value", "lo", "hi", "label"]
            body.append(_table(cols, [{k: s.get(k, "") for k in cols} for s in steps]))
        loop = plan.get("loop", "once")
        desc = {"once": "once", "repeat": f"{plan.get('count', 1)} times",
                "sweep": f"sweep x from {plan.get('start')} to {plan.get('stop')} step "
                         f"{plan.get('step')}"}.get(loop, loop)
        body.append(f"<p class='small'>Run {esc(desc)}; outputs off at the end: "
                    f"{'yes' if plan.get('off_at_end') else 'no'}; stop on first fail: "
                    f"{'yes' if plan.get('stop_on_fail') else 'no'}.</p>")
        if plan.get("use_script") and plan.get("script"):
            body.append(f"<pre>{esc(plan['script'])}</pre>")
    if log.strip():
        body.append("<h2>Log</h2>")
        body.append(f"<pre>{esc(log[-30000:])}</pre>")
    return _page(title, body)


def session_report(path: str, title: str = "", t_range: tuple[float, float] | None = None,
                   columns: list[tuple[int, str]] | None = None) -> str:
    """A session: instruments, one plot per numeric column (or the chosen
    (stream id, column) pairs), notes, captures, sequencer results."""
    from .session import SessionReader
    s = SessionReader(path)
    try:
        a, b = t_range or s.span()
        title = title or f"Session {Path(path).stem}"
        body = [f"<h1>{esc(title)}</h1>",
                f"<div class='sub'>{esc(s.wall(a))} to {esc(s.wall(b))} "
                f"({b - a:.1f} s) · {esc(Path(path).name)}</div>",
                "<h2>Instruments</h2><table class='kv'>"]
        body += [f"<tr><td>{esc(st.name)}</td><td>{esc(st.model)} · {esc(st.resource)} · "
                 f"{st.t.size} readings</td></tr>" for st in s.streams]
        body.append("</table>")
        notes = [(t - a, text) for t, text in s.notes if a <= t <= b]
        body.append("<h2>Plots</h2><p class='small'>Time is seconds from "
                    f"{esc(s.wall(a))}. Dashed lines are notes.</p>")
        want = set(columns) if columns else None
        for st in s.streams:
            if st.model == "sequence":
                continue
            keep = (st.t >= a) & (st.t <= b)
            for c, v in st.data.items():
                if want is not None and (st.id, c) not in want:
                    continue
                if want is None and c in ("logic", "current_min_A", "current_max_A"):
                    continue
                body.append(f"<p><b>{esc(st.name)}</b> · {esc(c)}</p>")
                body.append(svg_plot([(c, st.t[keep] - a, v[keep])], "time (s)", c,
                                     vlines=notes))
        seq = [st for st in s.streams if st.model == "sequence"]
        for st in seq:
            body.append(f"<h2>{esc(st.name)} results</h2>")
            rows = []
            for k in range(st.t.size):
                rows.append({c: (st.data[c][k] if c in st.data else "") for c in st.columns})
            body.append(_table(st.columns, rows))
        if notes:
            body.append("<h2>Notes</h2><table><tr><th>t (s)</th><th>Time</th><th>Note</th></tr>")
            body += [f"<tr><td class='num'>{t:.3f}</td><td>{esc(s.wall(t + a))}</td>"
                     f"<td>{esc(text)}</td></tr>" for t, text in notes]
            body.append("</table>")
        if s.captures:
            body.append("<h2>Logic captures</h2><table><tr><th>t (s)</th><th>Capture</th>"
                        "<th>Decoded rows</th><th>Alignment</th></tr>")
            for c in s.captures:
                n = len(c.load().table_rows)
                body.append(f"<tr><td class='num'>{c.t0 - a:.6f}</td><td>{esc(c.name)}</td>"
                            f"<td class='num'>{n}</td><td>{esc(c.note or 'host clock')}</td></tr>")
            body.append("</table>")
        return _page(title, body)
    finally:
        s.close()


def save(html: str, path: str):
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        try:
            from weasyprint import HTML
        except ImportError:
            raise RuntimeError("PDF needs WeasyPrint: re-run scripts/setup-ubuntu.sh (or setup-pi.sh), or "
                               ".venv/bin/pip install weasyprint. Save as .html instead, "
                               "and print that to PDF from a browser.") from None
        HTML(string=html).write_pdf(str(p))
    else:
        p.write_text(html, encoding="utf-8")
