# Multiple apps and Play developer accounts

One repo can release several apps, spread over several Google Play developer accounts.
Example: three apps on your company account and one on a partner's account.

Full example: [`examples/multi-account.release-bot.yml`](../examples/multi-account.release-bot.yml).

## Config

```yaml
defaults: { ...rollout, health, slack, access... }   # shared; any key can be overridden per app

accounts:
  vaazh:   { environment: play-vaazh }      # Play developer account #1
  partner: { environment: play-partner }    # Play developer account #2

apps:
  shop:        { account: vaazh,   package_name: com.vaazh.shop,  repository: vaazh-studios/shop-android }
  chat:        { account: vaazh,   package_name: com.vaazh.chat,  health: { min_distinct_users: 300 } }
  fitness:     { account: vaazh,   package_name: com.vaazh.fitness,
                 rollout: { targets: { tuesday: 0.20, wednesday: 0.50, thursday: 1.0 } } }
  partner-app: { account: partner, package_name: com.partner.app, slack: { channel_id: C_PARTNER } }
```

Per app you can set: `name`, `package_name`, `repository`, `tag_prefix`, `signing_environment`,
`build`, and override anything from `defaults` (`rollout`, `health`, `slack`, `access`, `play`).
Maps merge key by key, except `rollout.targets` and `access.release_heroes`, which an app
replaces as a whole (an app with its own schedule doesn't inherit the default's other days).

## What happens in the workflows

| Workflow | Multi-app behaviour |
|---|---|
| Submit, Halt, Resume, Inject incident | Take an **app** input. The job runs in that app's account environment |
| Rollout step, Health check (scheduled) | Fan out: one job per app, each in its account environment, each with its **own queue**, so one app's halt or slow run never blocks another. Manual runs can target one app |
| Grafana webhook | `client_payload.app` checks one app, otherwise all |
| Slack | Separate thread per app and version ("Shop Android 4.12.0"); channels can differ per app |

## Credentials: one environment and service account per Play account

1. For each Play developer account, create a service account (step 1 of [setup.md](setup.md)),
   invite it in **that** account's Play Console, scoped to that account's apps.
2. Create a GitHub environment per account (`play-vaazh`, `play-partner`), deployment branches
   `main`, each with its own `GCP_SERVICE_ACCOUNT` (and `GCP_WORKLOAD_IDENTITY_PROVIDER`) variable.
3. Bind each service account to its environment only:

   ```bash
   gcloud iam service-accounts add-iam-policy-binding release-bot-partner@PROJECT.iam.gserviceaccount.com \
     --role=roles/iam.workloadIdentityUser \
     --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.environment/play-partner"
   ```

   A job for a `vaazh` app can't impersonate the partner account's service account, and vice versa.
   The pool's provider condition (`assertion.environment.startsWith('play-')`) covers all of them.

The service accounts can live in different Google Cloud projects; one workload identity pool
can still issue tokens for all of them.

## Upload keys

Each app usually has its own upload key. Give each app a `signing_environment`
(`android-signing-shop`, …) holding the four `ANDROID_UPLOAD_*` secrets.

## Apps in other repositories

If an app's code lives in another repo, set `repository: org/app-repo`. The workflow checks out
the tag there, builds, and publishes the GitHub release there. It needs a token with
**Contents: read & write** on those repos, stored as the `RELEASE_BOT_REPO_TOKEN` secret. A
GitHub App installation token scoped to those repos is better than a personal token.

Tags: each app's tags live in its own repo (`v4.12.0`). In a monorepo, give apps a `tag_prefix`
(`shop/`), so `shop/v4.12.0` and `chat/v2.3.0` don't collide.

## Schedules

The rollout cron runs once a day and every app reads its own `rollout.targets`. Make sure the
cron covers every weekday any app uses (the default `0 7 * * 1-3` covers Mon–Wed; the `fitness`
example above needs Thursday too: `0 7 * * 1-4`).
