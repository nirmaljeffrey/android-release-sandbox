# Security

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security → Report a vulnerability) rather
than a public issue.

## Security model

| Concern | How it's handled |
|---|---|
| Google credentials | None stored. GitHub's OIDC token is exchanged for short-lived Google credentials via Workload Identity Federation. The provider's attribute condition only accepts this repository, the `play-production` environment and `refs/heads/main`. |
| Play access | One service account, invited to **one app** in Play Console with *Release to production* and *View app information*. |
| Crashlytics data | Read-only BigQuery access (`bigquery.dataViewer` on the export dataset, `bigquery.jobUser`). |
| Upload keystore | Lives in a separate `android-signing` environment. The build job never receives Play credentials; the Play jobs never see the keystore. The decoded keystore is deleted after the build. |
| Who can release | Submit and Resume check that the GitHub actor is mapped to a Slack user **currently** in `@android-release-hero`. Halt is open to anyone with repo access. Scheduled jobs run the reviewed code on `main`. |
| Tampering with workflows | Protect `main` (required reviews, CODEOWNERS on `.github/`, `release_bot/`, `release-bot.yml`) and restrict both environments to `main`, so a modified workflow on another branch gets no credentials. |
| Inbound traffic | None. The optional Grafana webhook only triggers a health check; the bot re-reads real data before halting, so a forged call can't halt on its own. |
| Workflow inputs | Passed to scripts via environment variables, never interpolated into shell. |
| Slack token | Bot scopes `chat:write`, `channels:history`, `channels:join`, `groups:history`, `usergroups:read`. Stored as a repo/environment secret. |

## Mock mode

`RELEASE_BOT_MOCK=true` replaces Google Play, Play Vitals, Crashlytics and Grafana with a JSON file
kept in the Actions cache. Never set it in a repo that also holds real credentials.
