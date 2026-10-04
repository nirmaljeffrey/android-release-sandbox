"""python -m sim [scenario ...] [--report out.html]

No scenario → run all. Set SLACK_BOT_TOKEN + SIM_SLACK_CHANNEL to also post
to a real Slack test channel (optional SIM_SLACK_ANNOUNCE_CHANNEL / SIM_SLACK_ALERTS_CHANNEL).
"""

import argparse
import sys
from pathlib import Path

from sim import runner


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sim")
    ap.add_argument("scenarios", nargs="*", help="scenario ids (file names in sim/scenarios) or paths")
    ap.add_argument("--report", default="sim/out/report.html")
    ap.add_argument("--no-slack", action="store_true", help="never post to real Slack")
    args = ap.parse_args(argv)

    names = args.scenarios or sorted(p.stem for p in runner.SCENARIOS.glob("*.yml"))
    real = None if args.no_slack else runner.real_slack_from_env()
    if real:
        print(f"Mirroring to real Slack channel {real['release']} (run {real['run_id']})")

    results = []
    for name in names:
        s = runner.load_scenario(name)
        print(f"\n━━ {s['title']} ━━")
        r = runner.run_scenario(s, real_slack=real, quiet=False)
        verdict = "PASS" if not r.failures else "FAIL: " + "; ".join(r.failures)
        print(f"   final: {r.final}  →  {verdict}")
        results.append(r)

    from sim import report
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.render(results))
    print(f"\nReport: {out}")
    return 1 if any(r.failures for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
