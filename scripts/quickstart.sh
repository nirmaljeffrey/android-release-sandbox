#!/usr/bin/env bash
# Turn a copy of this template into a working mock-mode demo.
# Usage: scripts/quickstart.sh [owner/repo]   (defaults to the current repo)
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
ME="$(gh api user --jq .login)"
echo "Setting up mock mode in $REPO (on-duty release hero: $ME)"

gh variable set RELEASE_BOT_MOCK --body true -R "$REPO"
gh variable set MOCK_ON_DUTY --body "$ME" -R "$REPO"

for env in play-production android-signing; do
  gh api -X PUT "repos/$REPO/environments/$env" --silent \
    -F "deployment_branch_policy[protected_branches]=false" \
    -F "deployment_branch_policy[custom_branch_policies]=true"
  gh api -X POST "repos/$REPO/environments/$env/deployment-branch-policies" --silent \
    -f name=main -f type=branch 2>/dev/null || true
done

for label in feature:1D76DB bug:D73A4A chore:C5DEF5; do
  gh label create "${label%%:*}" --color "${label##*:}" -R "$REPO" --force >/dev/null
done

cat <<MSG

Done. Next:
  1. Actions → "Sandbox · Create release tag" → 1.0.0, then again with 1.1.0
  2. Actions → "Android · Submit to Play" → v1.1.0
  3. After ~5 minutes: Actions → "Android · Rollout step" (each run = one step)
  4. Actions → "Sandbox · Inject incident" → new-crash  (watch it halt)

Optional Slack: create an app from slack-app-manifest.yml, then
  pbpaste | gh secret set SLACK_BOT_TOKEN -R $REPO
  gh variable set SLACK_CHANNEL_ID --body C0123456789 -R $REPO
MSG
