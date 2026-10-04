"""Render simulator results as one self-contained HTML page (Slack-style transcript)."""

import html
import re
from datetime import datetime

from sim.runner import END, parse_time

CHANNELS = [
    ("release", "#android-releases", "Release thread: every bot update"),
    ("announce", "#mobile-announcements", "Wider group"),
    ("alerts", "#slo-alerts", "Your SLO alerts channel"),
]


def mrkdwn(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"&lt;!subteam\^[A-Z0-9]+&gt;", '<span class="mention">@android-release-hero</span>', t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", t)
    t = re.sub(r"(https://[^\s<]+)", r'<a href="\1">\1</a>', t)
    return t.replace("\n", "<br>")


def _t(dt: datetime) -> str:
    return dt.strftime("%a %H:%M")


def chart(result) -> str:
    """Step chart of the rollout % over the week."""
    play = result.play
    start = parse_time(result.scenario.get("submit_at", "thu 18:00"))
    end = parse_time(result.scenario.get("end_at", END))
    span = (end - start).total_seconds() or 1
    W, H, L, R, T, B = 760, 170, 44, 12, 14, 30

    def x(dt):
        return L + (W - L - R) * max(0, min(1, (dt - start).total_seconds() / span))

    def y(f):
        return T + (H - T - B) * (1 - f)

    parts = []
    for f in (0, 0.2, 0.5, 1.0):
        parts.append(f'<line x1="{L}" x2="{W - R}" y1="{y(f):.1f}" y2="{y(f):.1f}" class="grid"/>'
                     f'<text x="{L - 6}" y="{y(f) + 4:.1f}" class="axis" text-anchor="end">{f:.0%}</text>')
    for day in ("fri", "sat", "sun", "mon", "tue", "wed"):
        dt = parse_time(f"{day} 00:00")
        if start < dt < end:
            parts.append(f'<line x1="{x(dt):.1f}" x2="{x(dt):.1f}" y1="{T}" y2="{H - B}" class="grid"/>'
                         f'<text x="{x(dt) + 4:.1f}" y="{H - B + 16}" class="axis">{day.title()}</text>')

    # Build segments: (from, to, fraction, status)
    points = []
    if play.approved_at:
        for t, _, status, f in play.history:
            points.append((max(t, play.approved_at) if t < play.approved_at else t, status, f))
        points.sort(key=lambda p: p[0])
    segs = []
    for i, (t, status, f) in enumerate(points):
        if t > end:
            break
        t_end = points[i + 1][0] if i + 1 < len(points) else end
        segs.append((t, min(t_end, end), f, status))
    if play.approved_at and play.approved_at > start:
        a = min(play.approved_at, end)
        parts.append(f'<line x1="{x(start):.1f}" x2="{x(a):.1f}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="review"/>'
                     f'<text x="{x(start) + 4:.1f}" y="{y(0) - 6:.1f}" class="axis">in Google review</text>')
    prev_y = None
    for t0, t1, f, status in segs:
        cls = "halted" if status == "halted" else "live"
        if prev_y is not None:
            parts.append(f'<line x1="{x(t0):.1f}" x2="{x(t0):.1f}" y1="{prev_y:.1f}" y2="{y(f):.1f}" class="{cls}"/>')
        parts.append(f'<line x1="{x(t0):.1f}" x2="{x(t1):.1f}" y1="{y(f):.1f}" y2="{y(f):.1f}" class="{cls}"/>')
        if status == "halted":
            parts.append(f'<text x="{x(t0) + 4:.1f}" y="{y(f) - 6:.1f}" class="axis halted-label">halted</text>')
        prev_y = y(f)
    return (f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" '
            f'aria-label="Rollout percentage over the week">{"".join(parts)}</svg>')


def runs_html(result) -> str:
    rows, quiet = [], []

    def flush():
        if quiet:
            rows.append(f'<li class="run quiet">⋯ {len(quiet)} quiet health checks '
                        f'({_t(quiet[0].at)} – {_t(quiet[-1].at)}): healthy or not enough data, nothing to say</li>')
            quiet.clear()

    for r in sorted(result.runs, key=lambda r: r.at):
        is_quiet = (r.workflow == "Android · Health check" and r.trigger == "schedule"
                    and not any(e.at == r.at for e in result.slack.entries))
        if is_quiet:
            quiet.append(r)
            continue
        flush()
        icon = "🟢" if r.workflow == "Google Play" else ("✅" if r.ok else "❌")
        log = f'<pre>{html.escape(r.output)}</pre>' if r.output else ""
        rows.append(f'<li class="run{" fail" if not r.ok else ""}"><details><summary>'
                    f'<span class="time">{_t(r.at)}</span> {icon} <b>{html.escape(r.workflow)}</b> '
                    f'<span class="trigger">{html.escape(r.trigger)}</span></summary>{log}</details></li>')
    flush()
    return f'<ul class="runs">{"".join(rows)}</ul>'


def slack_html(result) -> str:
    cols = []
    for kind, name, hint in CHANNELS:
        msgs = []
        for e in [e for e in result.slack.entries if e.channel == kind]:
            cls = "msg root" if e.root else ("msg reply" if e.thread else "msg")
            msgs.append(f'<div class="{cls}"><div class="who"><span class="bot">Release Bot</span>'
                        f'<span class="app">APP</span><span class="time">{_t(e.at)}</span></div>'
                        f'<div class="text">{mrkdwn(e.text)}</div></div>')
        body = "".join(msgs) or '<div class="empty">No messages</div>'
        cols.append(f'<section class="channel"><header><b>{name}</b><span>{hint}</span></header>{body}</section>')
    return f'<div class="slack">{"".join(cols)}</div>'


def render(results) -> str:
    summary = []
    sections = []
    for i, r in enumerate(results):
        s = r.scenario
        ok = not r.failures
        final = r.final
        frac = f" at {final['fraction']:.0%}" if final.get("fraction") is not None else ""
        badge = '<span class="pass">PASS</span>' if ok else f'<span class="fail">FAIL</span>'
        summary.append(f'<tr><td><a href="#{s["id"]}">{html.escape(s["title"])}</a></td>'
                       f'<td>{final["status"]}{frac}</td><td>{badge}</td></tr>')
        failures = "".join(f"<li>{html.escape(f)}</li>" for f in r.failures)
        sections.append(f'''
<details class="scenario" id="{s["id"]}" {"open" if i == 0 else ""}>
  <summary><span class="title">{html.escape(s["title"])}</span> {badge}
    <span class="final">{final["status"]}{frac}</span></summary>
  <p class="desc">{html.escape(s.get("description", ""))}</p>
  {f'<ul class="failures">{failures}</ul>' if failures else ""}
  {chart(r)}
  <div class="grid2">
    <div><h3>GitHub Actions runs</h3>{runs_html(r)}</div>
    <div><h3>Slack</h3>{slack_html(r)}</div>
  </div>
</details>''')

    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Release Week Simulator</title>
<style>
:root {{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#1d1c1d; --muted:#616061; --line:#e3e4e8;
  --accent:#1264a3; --ok:#007a5a; --bad:#e01e5a; --warn:#b26b00; --code:#f2f3f5; --mention:#e8f5fa;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#141518; --panel:#1b1d21; --ink:#e8e8e8; --muted:#a3a3a8; --line:#2c2f36;
  --accent:#4ea1e8; --ok:#2bac76; --bad:#f2557f; --warn:#e8a33b; --code:#262a31; --mention:#173445;
}} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Lato,sans-serif; }}
main {{ max-width:1200px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h3 {{ font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:16px 0 8px; }}
.lede {{ color:var(--muted); margin:0 0 20px; max-width:760px; }}
table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; margin-bottom:24px; }}
td {{ padding:8px 12px; border-top:1px solid var(--line); }} tr:first-child td {{ border-top:0; }}
a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.pass,.fail {{ font-size:11px; font-weight:700; padding:2px 6px; border-radius:4px; color:#fff; }}
.pass {{ background:var(--ok); }} .fail {{ background:var(--bad); }}
.scenario {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:4px 16px 16px; margin-bottom:16px; }}
.scenario > summary {{ cursor:pointer; padding:12px 0; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
.scenario .title {{ font-weight:700; font-size:16px; }} .final {{ color:var(--muted); font-size:12px; }}
.desc {{ color:var(--muted); margin:0 0 8px; }} .failures {{ color:var(--bad); }}
.chart {{ width:100%; height:auto; margin:4px 0 8px; }}
.chart .grid {{ stroke:var(--line); }} .chart .axis {{ fill:var(--muted); font-size:11px; }}
.chart .live {{ stroke:var(--accent); stroke-width:3; }} .chart .halted {{ stroke:var(--bad); stroke-width:3; }}
.chart .review {{ stroke:var(--muted); stroke-width:2; stroke-dasharray:4 4; }} .chart .halted-label {{ fill:var(--bad); }}
.grid2 {{ display:grid; grid-template-columns: minmax(0,5fr) minmax(0,7fr); gap:20px; }}
@media (max-width: 900px) {{ .grid2 {{ grid-template-columns:1fr; }} }}
.runs {{ list-style:none; margin:0; padding:0; }}
.run {{ border:1px solid var(--line); border-radius:6px; padding:6px 10px; margin-bottom:6px; }}
.run.fail {{ border-color:var(--bad); }} .run.quiet {{ color:var(--muted); font-size:12px; border-style:dashed; }}
.run summary {{ cursor:pointer; }} .run .time {{ font-variant-numeric:tabular-nums; color:var(--muted); margin-right:4px; }}
.run .trigger {{ color:var(--muted); font-size:12px; }}
.run pre {{ white-space:pre-wrap; background:var(--code); padding:8px; border-radius:4px; font-size:12px; margin:8px 0 0; }}
.slack {{ display:flex; flex-direction:column; gap:12px; }}
.channel {{ border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
.channel header {{ padding:8px 12px; border-bottom:1px solid var(--line); display:flex; gap:8px; align-items:baseline; }}
.channel header span {{ color:var(--muted); font-size:12px; }}
.msg {{ padding:8px 12px; }} .msg + .msg {{ border-top:1px solid var(--line); }}
.msg.reply {{ margin-left:24px; border-left:3px solid var(--line); }}
.who {{ display:flex; gap:6px; align-items:center; font-size:13px; }}
.bot {{ font-weight:700; }} .app {{ font-size:9px; background:var(--code); color:var(--muted); padding:1px 4px; border-radius:3px; }}
.who .time {{ color:var(--muted); font-size:12px; }}
.text {{ margin-top:2px; overflow-wrap:anywhere; }}
.text code {{ background:var(--code); border:1px solid var(--line); border-radius:3px; padding:0 3px; font-size:12px; color:var(--bad); }}
.mention {{ background:var(--mention); color:var(--accent); border-radius:3px; padding:0 2px; font-weight:600; }}
.empty {{ color:var(--muted); padding:10px 12px; font-size:12px; }}
</style></head>
<body><main>
<h1>Release week simulator</h1>
<p class="lede">Each scenario replays Thu 18:00 → Wed 12:00 (UTC) with mock Play, Play Vitals, Crashlytics and Grafana data.
The decisions and Slack messages come from the real <code>release_bot</code> code your GitHub Actions run.</p>
<table>{"".join(summary)}</table>
{"".join(sections)}
</main></body></html>'''
