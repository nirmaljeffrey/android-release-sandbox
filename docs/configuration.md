# Configuring rollouts and health rules

Everything lives in `release-bot.yml`. Put shared settings under `defaults:`; any app (team) can
override `rollout` and `health` with its own. Check your changes before merging:

```bash
python -m release_bot validate     # fails on anything impossible or malformed (also runs in CI)
python -m release_bot plan         # each app's timeline per platform + its health rules
```

## Rollout schedule

```yaml
rollout:
  platforms: aligned        # aligned | separate
  schedule:
    - {on: monday,    percent: 1}
    - {on: tuesday,   percent: 2}
    - {on: wednesday, percent: 100}
  when_data_is_thin: hold   # hold | advance
```

| Key | Meaning |
|---|---|
| `schedule` | Steps in order, as many as you like. Percentages must increase |
| `on` | A weekday (`monday`), or `day N` = N days after the submit day (`day 0` is submit day) |
| first step | Goes live when the store approves the release |
| later steps | Allowed from their day on, **only if health is green**, at most one step per run. A held day shifts the rest |
| `when_data_is_thin` | Too few users on the new version to judge (`health.min_users`): `hold` waits, `advance` moves on if nothing else is wrong |
| `platforms` | `aligned`: Android and iOS share `schedule`. `separate`: `android: {schedule: …}` and `ios: {schedule: …}` |

Weekday or day-based? Weekdays suit a weekly release train and must fit within one week, in
order (`friday, monday, tuesday` is fine). `day N` suits longer rollouts, or "N days of rollout
whenever we submit". Use one style per schedule. Day-based steps count from the time the submit
workflow created the GitHub release.

**Make sure the rollout cron covers every weekday you use.** The default `0 7 * * 1-3` runs
Mon–Wed; a `day N` schedule usually wants a daily cron (`0 7 * * *`).

### Android vs iOS

| | Android (Play) | iOS (App Store), coming soon |
|---|---|---|
| Percentages | Any | Apple's phased release only: day 1 **1%**, day 2 **2%**, day 3 **5%**, day 4 **10%**, day 5 **20%**, day 6 **50%**, day 7 **100%** |
| What the bot does | Sets each step | Starts the phased release, **pauses** it if unhealthy, **releases to everyone** at a 100% step |
| First step | Live as soon as Google approves | Starts on its day (the build is submitted with manual release) |

`validate` rejects an iOS schedule Apple can't follow, and says what to use instead. With
`platforms: aligned`, your shared schedule has to work for iOS too, so it's checked against
Apple's days. Pick `separate` if you want Android to move faster than Apple allows.

Today Android's first step goes live on approval: Play can't hold an approved release until a
given day yet. `plan` warns when your first step names a later day.

### Recipes

**Weekly train, Apple-aligned** (submit Thursday, 1% Mon, 2% Tue, everyone Wed):
```yaml
rollout:
  platforms: aligned
  schedule:
    - {on: monday,    percent: 1}
    - {on: tuesday,   percent: 2}
    - {on: wednesday, percent: 100}
```

**Faster Android, Apple's pace on iOS:**
```yaml
rollout:
  platforms: separate
  android:
    schedule:
      - {on: friday,    percent: 2}
      - {on: monday,    percent: 20}
      - {on: tuesday,   percent: 50}
      - {on: wednesday, percent: 100}
  ios:
    schedule:
      - {on: monday,    percent: 1}
      - {on: tuesday,   percent: 2}
      - {on: wednesday, percent: 100}
```

**Five-day rollout, whenever we submit:**
```yaml
rollout:
  schedule:
    - {on: day 1, percent: 5}
    - {on: day 3, percent: 25}
    - {on: day 5, percent: 100}
```

**Cautious team with a small user base, over ~two weeks:**
```yaml
rollout:
  when_data_is_thin: hold
  schedule:
    - {on: day 4,  percent: 1}
    - {on: day 6,  percent: 5}
    - {on: day 8,  percent: 20}
    - {on: day 12, percent: 100}
health:
  min_users: 200
```

## Health rules

```yaml
health:
  min_users: 1000
  sources:                  # where signals come from; remove one to turn it off
    play_vitals: {}
    crashlytics: {project: my-firebase, dataset: firebase_crashlytics, table: com_app_ANDROID_REALTIME, lookback_days: 30}
    grafana: {matchers: ['team="mobile"']}
  rules:
    - {name: Google ANR line, source: play_vitals, metric: user_perceived_anr_rate, above: "0.47%", action: halt}
    - {name: ANR regression,  source: play_vitals, metric: user_perceived_anr_rate, above_previous_by: "25%", action: hold}
    - {name: New crash,       source: crashlytics, metric: new_fatal_issue_users, at_least: 25, action: halt}
    - {name: Pager alert,     source: grafana, severity: critical, action: halt}
    - {name: Latency,         source: grafana, severity: warning, alert: "latency", action: notify}
```

### Actions

| Action | Effect |
|---|---|
| `halt` | Stops the rollout now (Play: halted; iOS: phased release paused). Pings the release hero, SLO alerts and the wider channel. Only a human resumes |
| `hold` | Doesn't advance; asks for a human look in the thread and SLO alerts (once, not every run) |
| `notify` | Posts an FYI and keeps rolling out |

A source that can't be reached counts as `hold`: the bot can't prove the release is healthy, but an
outage elsewhere never halts it.

### What you can measure

| Source | `metric` | Threshold keys |
|---|---|---|
| `play_vitals` | `user_perceived_anr_rate`, `user_perceived_crash_rate`, `anr_rate`, `crash_rate` | `above: "0.47%"` (absolute) **or** `above_previous_by: "25%"` (vs the previous version) |
| `crashlytics` | `new_fatal_issue_users`: users hit by a fatal crash group first seen in this build | `at_least: 25` |
| `grafana` | firing alerts matching `sources.grafana.matchers` | `severity: critical` (or a list), optional `alert:` regex on the alert name |

Percentages are written as strings with `%` (`"0.47%"`); plain numbers are read as fractions
(`0.0047`). Google's own "bad behaviour" lines are 0.47% (ANR) and 1.09% (crashes), counted on
user-perceived rates.

Play Vitals data is 1–2 days old and needs `min_users` on the new version; Crashlytics and
Grafana are near real time. A good rule set halts on Crashlytics and Grafana quickly, and uses
Vitals to stop the larger steps.

## Per team

```yaml
apps:
  checkout:
    account: main
    package_name: com.example.checkout
    platforms: [android, ios]          # validate checks the schedule fits iOS too
    # uses defaults.rollout and defaults.health
  growth:
    account: main
    package_name: com.example.growth
    rollout:
      schedule: [{on: day 1, percent: 5}, {on: day 3, percent: 25}, {on: day 5, percent: 100}]
    health:
      min_users: 200
      rules:                           # a list replaces the default list
        - {name: Strict ANR, source: play_vitals, metric: user_perceived_anr_rate, above: "0.30%", action: halt}
        - {name: Any new crash, source: crashlytics, at_least: 5, action: halt}
```

Merging: maps merge key by key (so `sources: {crashlytics: {table: …}}` only changes the table);
lists (`schedule`, `rules`) and `rollout.targets` replace the default as a whole.

## Older format

Configs with `rollout.steps` + `rollout.targets` + `play.initial_fraction`, and per-source
thresholds under `health.play_vitals.halt/hold`, `health.crashlytics.new_issue_min_users` and
`health.grafana.*_severities`, still work and are converted to the format above.
