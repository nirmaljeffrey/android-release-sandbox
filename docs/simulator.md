# Simulator

`sim/` replays a full release week (Thu 18:00 → Wed 12:00) against mock Play, Play Vitals,
Crashlytics and Grafana. The decisions and Slack messages come from the same `release_bot`
code the workflows run; only the outside services are faked.

```bash
python -m sim                       # all scenarios → sim/out/report.html
python -m sim 02-crash-spike-at-20  # just one
```

| Scenario | What it proves |
|---|---|
| `01-happy-path` | 2% → 20% → 50% → 100% with no human after Thursday |
| `02-crash-spike-at-20` | A new Crashlytics crash halts at 20% within 3h |
| `03-anr-regression-hold` | A relative ANR regression holds at 2% and warns **once**, not every 3h |
| `04-google-anr-threshold-weekend` | Google's ANR line crossed on Saturday → halted on Sunday, unattended |
| `05-grafana-alert-then-resume` | Grafana webhook halts in minutes; the hero resumes; the plan shifts one day |
| `06-slow-google-review` | No data yet → hold; then one step per day |
| `07-not-on-duty` | Someone outside `@android-release-hero` can't submit |
| `08-previous-rollout-unfinished` | Submit is blocked while last week's release is still rolling out |

Write your own: copy a YAML file in `sim/scenarios/` and change the numbers. Times are
`"<day> HH:MM"` in UTC, and `expect:` makes it a regression test (`pytest` runs them all).

**Post to a real Slack test channel:** create a Slack app in a test workspace (scopes `chat:write`,
`channels:history`), invite it to a channel, then run this in your own terminal:

```bash
export SLACK_BOT_TOKEN=xoxb-...      # test workspace only
export SIM_SLACK_CHANNEL=C0TESTCHAN  # optional: SIM_SLACK_ANNOUNCE_CHANNEL, SIM_SLACK_ALERTS_CHANNEL
python -m sim 05-grafana-alert-then-resume
```

Each message is stamped with its simulated time, and each run gets its own thread.
The on-duty check always uses the scenario's `on_duty`, so the test workspace doesn't need an
`@android-release-hero` group.
