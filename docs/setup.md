# Production setup (Google Play)

Do this once per app. Budget an hour. Before relying on the bot, run it in shadow (dry-run) mode for a release or two — see step 7.

## 1. Google Cloud: keyless login for GitHub Actions

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
  --attribute-condition="assertion.repository=='ORG/REPO' && assertion.environment.startsWith('play-') && assertion.ref=='refs/heads/main'"

gcloud iam service-accounts create android-release-bot

gcloud iam service-accounts add-iam-policy-binding \
  android-release-bot@PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.environment/play-production"
```

The binding is per **environment**: only jobs running in `play-production` can act as this service
account. With several Play developer accounts, repeat the service-account steps once per account
and bind each one to its own environment (see [multi-app.md](multi-app.md)).

**Crashlytics (BigQuery):** in the Firebase project, give the service account `roles/bigquery.dataViewer` on the `firebase_crashlytics` dataset and `roles/bigquery.jobUser` on the project.

## 2. Play Console

**Users and permissions → Invite new users →** the service account email. Under **App permissions**, add **only this app** with:
- *View app information and download bulk reports (read-only)*, needed for the Vitals API
- *Release to production, exclude devices, and use Play App Signing*

Also turn **off Managed publishing**, or approved changes will wait for a manual "Publish".

## 3. Slack app

- **Bot scopes:** `chat:write`, `channels:history` (`groups:history` for private channels), `usergroups:read`.
- **Invite the bot to** the release channel, the wider announcement channel and the SLO alerts channel.
- **Copy the IDs** of those channels and of the `@android-release-hero` user group into `release-bot.yml`.
- **Map each possible hero** in `access.release_heroes` (`github-login: SLACK_USER_ID`).

## 4. Grafana

- **API access:** create a service account with the Viewer role and save its token as `GRAFANA_TOKEN`.
- **Labels:** label the alert rules that matter for a release with `team="mobile"` and `severity="critical"` (halt) or `"warning"` (hold). An `app_version` label makes alerts much more precise.
- **Optional instant halt:** add a webhook contact point (routed for `team=mobile, severity=critical`, next to your SLO channel):
  - URL: `https://api.github.com/repos/ORG/REPO/dispatches`, method POST
  - Authorization: `Bearer <fine-grained token, this repo only, Contents: write>`
  - Custom payload: `{"event_type":"rollout-alert","client_payload":{"alertname":"{{ .CommonLabels.alertname }}"}}`
  - This needs a Grafana version with custom webhook payloads. Without it, skip this; the 3-hour check still catches the alert.
  - The payload is only used as a label. The bot re-checks real data before halting, so a forged call can't halt anything on its own.

## 5. GitHub repository settings

| Where | Name | Value |
|---|---|---|
| Environment **`play-production`** (deployment branches: `main`) | var `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github-actions` |
| | var `GCP_SERVICE_ACCOUNT` | `android-release-bot@PROJECT_ID.iam.gserviceaccount.com` |
| | var `GRAFANA_URL` | `https://grafana.example.com` |
| | secret `SLACK_BOT_TOKEN` | `xoxb-…` |
| | secret `GRAFANA_TOKEN` | Grafana service account token |
| Environment **`android-signing`** (deployment branches: `main`) | secret `ANDROID_UPLOAD_KEYSTORE_BASE64` | `base64 -i upload.jks` |
| | secrets `ANDROID_UPLOAD_STORE_PASSWORD`, `ANDROID_UPLOAD_KEY_ALIAS`, `ANDROID_UPLOAD_KEY_PASSWORD` | |
| Repo variable | `RELEASE_BOT_ENABLED` | `true`: lets the scheduled rollout and health runs start (they stay idle until set) |

Build settings (Gradle directory, bundle task, `.aab` location, JDK) live in `release-bot.yml`
under each app's `build:`, not in repo variables.

The keystore must be your Play App Signing **upload key**. Flutter: replace the Gradle step with `flutter build appbundle`.

## 6. `release-bot.yml`

Set `package_name`, the Crashlytics table (`<package_with_underscores>_ANDROID_REALTIME`), the channel IDs and the heroes. **Tune `health.min_distinct_users`:** at 2%, an app with 100k daily users only gives about 2k users on the new version. If the minimum is higher than that, Monday will always hold.

## 7. Dry run during your next manual release

1. Copy `release_bot/`, `.github/` and `release-bot.yml` into your Android repo (keep the paths), or start from **Use this template**. Leave out `examples/`, `sim/` and the `sandbox-*.yml` workflows if you don't want them.
2. Before relying on the bot, run **"Android · Health check"** with `dry_run: true` while a release is rolling out.
3. Compare the scorecard numbers with **Play Console → Android vitals**. In particular, check that crash and ANR rates come back as fractions (`0.0109` = 1.09%).
4. Run **"Android · Rollout step"** with `dry_run: true` on a Monday to see what it *would* do.

---

## Commands (local)

```bash
pip install -r release_bot/requirements.txt pytest
python -m pytest -q tests
gcloud auth application-default login   # if you want to try read-only commands locally
python -m release_bot status
python -m release_bot --dry-run check
```
