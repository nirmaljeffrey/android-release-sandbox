# Android release sandbox (mock mode)

A toy Android app plus the real release workflows, running in **mock mode**: Google Play,
Play Vitals, Crashlytics and Grafana are faked; everything else is real — GitHub Actions,
the build, GitHub release notes, schedules and Slack.

| Repo variable | Value |
|---|---|
| `RELEASE_BOT_MOCK` | `true` (turns on mock Play + mock health data) |
| `MOCK_ON_DUTY` | GitHub login(s) allowed to submit/resume, comma-separated (stands in for `@android-release-hero`) |
| `SLACK_CHANNEL_ID` | Release thread channel (optional: `SLACK_ANNOUNCE_CHANNEL_ID`, `SLACK_ALERTS_CHANNEL_ID`) |
| secret `SLACK_BOT_TOKEN` | Bot token from your test Slack workspace. Without it, Slack messages appear in the run logs |

**Differences from production:** every day is a rollout day, the rollout step runs every 30 min
and the health check every 15 min, Google "review" takes 5 minutes, and the bundle is unsigned.

## Try it

1. **Actions → Sandbox · Create release tag** → `1.2.0` (stands in for your CI's tagging)
2. **Actions → Android · Submit to Play** → tag `v1.2.0` (only someone in `MOCK_ON_DUTY` can run it)
3. Wait. Every 30 min the rollout moves one step (2% → 20% → 50% → 100%), or run
   **Android · Rollout step** yourself.
4. Break it: **Sandbox · Inject incident** → `new-crash` / `anr-over-threshold` / `grafana-critical`
   halts; `anr-regression` / `grafana-warning` holds. Pick `none` to clear it, then **Android · Resume rollout**.
5. Try submitting while a rollout is still running, or from an account not in `MOCK_ON_DUTY`.

The mock Play state lives in the Actions cache (`mock-play-state-*`). To start fresh,
delete those caches under **Actions → Caches**.

---

# Android release automation (Google Play)

GitHub Actions + a small Python bot that runs your weekly Play release:

| When | Who | What happens |
|---|---|---|
| **Thu evening** | `@android-release-hero` | Smoke test the Firebase build, then **Actions → "Android · Submit to Play" → Run workflow** with the tag. The bot checks you're on duty, builds a signed `.aab` from the tag, writes GitHub release notes since the previous tag, uploads to Play production at **2%**, opens a Slack thread and announces in the wider channel. |
| **Fri** | nobody | Google approves → 2% goes live by itself. |
| **Every 3h** | bot | Health check (Crashlytics, Play Vitals, Grafana). Hard signal → **automatic halt** + `@android-release-hero` ping in the thread and the SLO alerts channel. |
| **Mon / Tue / Wed 07:00 UTC** | bot | If healthy: 2% → **20%** → **50%** → **100%**. If not sure: holds and says why. If unhealthy: halts. |
| **Any time** | anyone | **"Android · HALT rollout"**. Resuming is release-hero only. |

Nothing is hosted. Nobody is on call for the tooling.

---

## How a rollout runs over several days

There is **no long-running job**. Each workflow run is short (about 1 minute) and stateless:

```
cron fires ─▶ read the production track from Play ─▶ check health ─▶ write one change to Play ─▶ exit
```

**Google Play is the database.** The track already stores which version is rolling out, its status (`inProgress` / `halted` / `completed`) and the current `userFraction`. Every run reads that, decides, and writes at most one change. So:

- Monday's run sees `4.12.0 inProgress 2%`, the target for Monday is 20%, health is green, and it sets 20%.
- If Monday held (for example, not enough users yet), Tuesday's run sees 2% and moves **one** step to 20%, not 50%. A held day shifts the plan by a day; it never jumps.
- A halted release is never touched by the schedule. Only a human resumes it.
- The Slack thread is found again by a marker in its root message, so that needs no state either.

GitHub Actions cost: roughly 60 one-minute Linux runs a week.

---

## Who can do what

| Action | Allowed | How it's enforced |
|---|---|---|
| Submit, Resume | The person in **`@android-release-hero` right now** | `release_bot authorize` asks Slack for the user group's members and maps GitHub login → Slack ID (`access.release_heroes` in `release-bot.yml`). Rotate the Slack group and the permission moves with it. |
| Halt | Anyone with repo access | Stopping a rollout is always safe. |
| Rollout steps, auto-halts | The bot (cron) | Always runs the reviewed code on `main`. |

The hero check is only trustworthy if nobody can run a modified copy of the workflow with Play credentials. Lock that down like this:

1. **Branch protection on `main`** (PR review required). Add **CODEOWNERS** for `.github/workflows/`, `release_bot/` and `release-bot.yml`.
2. **Environment `play-production` → Deployment branches: `main` only.** Its secrets and the Google login are unavailable to runs from any other branch.
3. **The Workload Identity Federation condition** (below) only accepts tokens from this repo, environment `play-production` and `refs/heads/main`.
4. **No JSON keys anywhere.** Google access uses short-lived OIDC tokens. The upload keystore lives in a separate `android-signing` environment, and the build job never sees Play credentials.

---

## Setup

### 1. Google Cloud: keyless login for GitHub Actions

Use the GCP project linked to Play Console (or any project) and enable the APIs:

```bash
gcloud services enable androidpublisher.googleapis.com playdeveloperreporting.googleapis.com \
  bigquery.googleapis.com iamcredentials.googleapis.com sts.googleapis.com
```

Create the workload identity pool, the OIDC provider and the service account:

```bash
gcloud iam workload-identity-pools create github --location=global

gcloud iam workload-identity-pools providers create-oidc github-actions \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.environment=assertion.environment,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='ORG/REPO' && assertion.environment=='play-production' && assertion.ref=='refs/heads/main'"

gcloud iam service-accounts create android-release-bot

gcloud iam service-accounts add-iam-policy-binding \
  android-release-bot@PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/ORG/REPO"
```

**Crashlytics (BigQuery):** in the Firebase project, give the service account `roles/bigquery.dataViewer` on the `firebase_crashlytics` dataset and `roles/bigquery.jobUser` on the project.

### 2. Play Console

**Users and permissions → Invite new users →** the service account email. Under **App permissions**, add **only this app** with:
- *View app information and download bulk reports (read-only)*, needed for the Vitals API
- *Release to production, exclude devices, and use Play App Signing*

Also turn **off Managed publishing**, or approved changes will wait for a manual "Publish".

### 3. Slack app

- **Bot scopes:** `chat:write`, `channels:history` (`groups:history` for private channels), `usergroups:read`.
- **Invite the bot to** the release channel, the wider announcement channel and the SLO alerts channel.
- **Copy the IDs** of those channels and of the `@android-release-hero` user group into `release-bot.yml`.
- **Map each possible hero** in `access.release_heroes` (`github-login: SLACK_USER_ID`).

### 4. Grafana

- **API access:** create a service account with the Viewer role and save its token as `GRAFANA_TOKEN`.
- **Labels:** label the alert rules that matter for a release with `team="mobile"` and `severity="critical"` (halt) or `"warning"` (hold). An `app_version` label makes alerts much more precise.
- **Optional instant halt:** add a webhook contact point (routed for `team=mobile, severity=critical`, next to your SLO channel):
  - URL: `https://api.github.com/repos/ORG/REPO/dispatches`, method POST
  - Authorization: `Bearer <fine-grained token, this repo only, Contents: write>`
  - Custom payload: `{"event_type":"rollout-alert","client_payload":{"alertname":"{{ .CommonLabels.alertname }}"}}`
  - This needs a Grafana version with custom webhook payloads. Without it, skip this; the 3-hour check still catches the alert.
  - The payload is only used as a label. The bot re-checks real data before halting, so a forged call can't halt anything on its own.

### 5. GitHub repository settings

| Where | Name | Value |
|---|---|---|
| Environment **`play-production`** (deployment branches: `main`) | var `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github-actions` |
| | var `GCP_SERVICE_ACCOUNT` | `android-release-bot@PROJECT_ID.iam.gserviceaccount.com` |
| | var `GRAFANA_URL` | `https://grafana.example.com` |
| | secret `SLACK_BOT_TOKEN` | `xoxb-…` |
| | secret `GRAFANA_TOKEN` | Grafana service account token |
| Environment **`android-signing`** (deployment branches: `main`) | secret `ANDROID_UPLOAD_KEYSTORE_BASE64` | `base64 -i upload.jks` |
| | secrets `ANDROID_UPLOAD_STORE_PASSWORD`, `ANDROID_UPLOAD_KEY_ALIAS`, `ANDROID_UPLOAD_KEY_PASSWORD` | |
| Repo variables (optional) | `ANDROID_PROJECT_DIR` | `android` for React Native; default `.` |
| | `ANDROID_BUNDLE_TASK` | default `:app:bundleRelease` (for example `:app:bundleProdRelease` with flavors) |
| | `ANDROID_AAB_GLOB` | default `app/build/outputs/bundle/release/*.aab` |
| | `ANDROID_JAVA_VERSION` | default `17` |

The keystore must be your Play App Signing **upload key**. Flutter: replace the Gradle step with `flutter build appbundle`.

### 6. `release-bot.yml`

Set `package_name`, the Crashlytics table (`<package_with_underscores>_ANDROID_REALTIME`), the channel IDs and the heroes. **Tune `health.min_distinct_users`:** at 2%, an app with 100k daily users only gives about 2k users on the new version. If the minimum is higher than that, Monday will always hold.

### 7. Dry run during your next manual release

1. Copy everything into your Android repo (keep the paths).
2. Before relying on the bot, run **"Android · Health check"** with `dry_run: true` while a release is rolling out.
3. Compare the scorecard numbers with **Play Console → Android vitals**. In particular, check that crash and ANR rates come back as fractions (`0.0109` = 1.09%).
4. Run **"Android · Rollout step"** with `dry_run: true` on a Monday to see what it *would* do.

---

## What it checks

| Source | Signal | Effect |
|---|---|---|
| Crashlytics (BigQuery streaming) | Fatal crash group first seen in this build, affecting ≥ `new_issue_min_users` users | **Halt** |
| Play Vitals (Reporting API) | User-perceived crash rate ≥ 1.09% or ANR rate ≥ 0.47% (Google's bad-behaviour lines) | **Halt** |
| | Crash or ANR rate > 1.25× the previous version | Hold |
| | Fewer than `min_distinct_users` daily users yet | Hold (not enough data) |
| Grafana | Matching alert firing with `severity=critical` | **Halt** |
| | … with `severity=warning` | Hold |
| Any source erroring | can't verify | Hold, never halt |

Good to know:
- **Play Vitals data is 1–2 days old,** so Monday's decision for 20% uses the weekend's data at 2%. Crashlytics and Grafana are near real time.
- **Crashlytics' export has no session counts,** so it's used to spot new crashes; crash and ANR *rates* come from Play Vitals.
- **There's no rollback on Play.** A halt stops new users from getting the update; users who already have it keep it. Fix forward with a higher `versionCode`.

## Simulator: try it with mock data

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

## Commands (local)

```bash
pip install -r release_bot/requirements.txt pytest
python -m pytest -q tests
gcloud auth application-default login   # if you want to try read-only commands locally
python -m release_bot status
python -m release_bot --dry-run check
```
