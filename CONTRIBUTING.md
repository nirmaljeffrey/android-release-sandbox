# Contributing

Thanks for helping! Issues and PRs are welcome, especially new health sources and iOS support.

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r release_bot/requirements.txt pytest
python -m pytest -q          # unit tests, mock-mode end-to-end, every simulator scenario
python -m sim                # replay release weeks → sim/out/report.html
```

Workflows are linted with [actionlint](https://github.com/rhysd/actionlint) (`pip install actionlint-py`).

## Layout

| Path | What |
|---|---|
| `release_bot/cli.py` | Commands the workflows run (`submit`, `check`, `advance`, `halt`, …) |
| `release_bot/gate.py` | Pure health logic: signals → OK / NOT_ENOUGH_DATA / HOLD / HALT |
| `release_bot/schedule.py` | Pure rollout-step logic |
| `release_bot/play.py`, `vitals.py`, `crashlytics.py`, `grafana.py`, `slack.py` | One client per external service |
| `release_bot/mock.py` | Mock mode for template repos |
| `sim/` | Release-week simulator and scenarios |
| `.github/workflows/android-*.yml` | The production workflows |

## Adding a health source (e.g. Sentry, Datadog)

1. **Client** `release_bot/<source>.py`: one method returning raw data for a version code.
2. **Evaluator** in `gate.py`: `evaluate_<source>(data, cfg) -> list[Finding]`. Keep it pure.
3. **Wire it** in `cli.collect_health()` and `build_deps()`, behind `health.<source>.enabled`.
4. **Mock it** in `release_bot/mock.py` and `sim/fakes.py`, and add a scenario in `sim/scenarios/`
   with an `expect:` block. That scenario is your regression test.
5. **Document** it in `docs/setup.md` and `docs/how-it-works.md`.

A source that errors must produce a HOLD finding, never a HALT.

## Bug reports

The fastest fix starts with a simulator scenario that reproduces the problem: copy one from
`sim/scenarios/`, change the numbers until it misbehaves, and attach it to the issue.
