"""release-bot.yml loading.

Two formats:

* **Multi-app** — `defaults`, `accounts` (one per Google Play developer
  account) and `apps`. Each app names its account and can override any default.
* **Single-app** (legacy) — everything at the top level. Treated as one app
  called "app" on an account whose environment is `play-production`.

`resolve()` returns one flat dict for a single app, so the rest of the bot
never needs to know which format was used.
"""

import copy
import os
import re
from pathlib import Path

import yaml

DEFAULT_PATH = Path("release-bot.yml")
LEGACY_APP_ID = "app"

# Repo variables can override Slack IDs without editing release-bot.yml.
# They apply to the defaults; an app that sets its own channel keeps it.
ENV_OVERRIDES = {
    "SLACK_CHANNEL_ID": "channel_id",
    "SLACK_ANNOUNCE_CHANNEL_ID": "announce_channel_id",
    "SLACK_ALERTS_CHANNEL_ID": "alerts_channel_id",
    "SLACK_HERO_USERGROUP_ID": "hero_usergroup_id",
}

BUILD_DEFAULTS = {
    "project_dir": ".",
    "bundle_task": ":app:bundleRelease",
    "aab_glob": "app/build/outputs/bundle/release/*.aab",
    "java_version": "17",
}


class ConfigError(ValueError):
    pass


def load(path: str | Path = DEFAULT_PATH) -> dict:
    """Raw config (either format) with env overrides applied to the defaults."""
    with open(path) as f:
        raw = yaml.safe_load(f)
    defaults = raw.setdefault("defaults", {}) if "apps" in raw else raw
    slack = defaults.setdefault("slack", {})
    for env, key in ENV_OVERRIDES.items():
        if os.environ.get(env):
            slack[key] = os.environ[env]
    return raw


def is_multi_app(raw: dict) -> bool:
    return "apps" in raw


def app_ids(raw: dict) -> list[str]:
    return list(raw["apps"]) if is_multi_app(raw) else [LEGACY_APP_ID]


# Maps that an app replaces wholesale instead of merging key by key: an app
# with its own schedule must not inherit the default schedule's other days.
REPLACE_KEYS = {"targets", "release_heroes"}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        deep = isinstance(v, dict) and isinstance(out.get(k), dict) and k not in REPLACE_KEYS
        out[k] = _merge(out[k], v) if deep else copy.deepcopy(v)
    return out


def resolve(raw: dict, app_id: str | None = None) -> dict:
    """Flat config for one app: package_name, play, rollout, health, slack, access,
    plus app_id, display_name, account, environment, signing_environment,
    repository, tag_prefix, tag_pattern and build."""
    if not is_multi_app(raw):
        if app_id not in (None, LEGACY_APP_ID):
            raise ConfigError(f"unknown app '{app_id}': release-bot.yml defines a single app")
        cfg = copy.deepcopy(raw)
        cfg.update(app_id=LEGACY_APP_ID, display_name="Android", account="default",
                   environment="play-production", signing_environment="android-signing",
                   repository="", tag_prefix=cfg.get("tag_prefix", ""))
        cfg["tag_pattern"] = cfg.get("tag_pattern") or f"{cfg['tag_prefix']}v*"
        cfg["build"] = _merge(BUILD_DEFAULTS, cfg.get("build", {}))
        return cfg

    apps = raw["apps"]
    if app_id is None:
        if len(apps) != 1:
            raise ConfigError(f"several apps configured, pass --app (one of: {', '.join(apps)})")
        app_id = next(iter(apps))
    if app_id not in apps:
        raise ConfigError(f"unknown app '{app_id}' (configured: {', '.join(apps)})")

    app = apps[app_id] or {}
    account_id = app.get("account")
    accounts = raw.get("accounts", {})
    if account_id not in accounts:
        raise ConfigError(f"app '{app_id}' uses account '{account_id}', which isn't under accounts:")
    account = accounts[account_id] or {}

    reserved = {"account", "name"}
    cfg = _merge(raw.get("defaults", {}), {k: v for k, v in app.items() if k not in reserved})
    if not cfg.get("package_name"):
        raise ConfigError(f"app '{app_id}' has no package_name")
    cfg.update(
        app_id=app_id,
        display_name=f"{app.get('name') or app_id.replace('-', ' ').title()} Android",
        account=account_id,
        environment=account.get("environment") or f"play-{account_id}",
        signing_environment=app.get("signing_environment") or "android-signing",
        repository=app.get("repository", ""),
        tag_prefix=app.get("tag_prefix", ""),
    )
    cfg["tag_pattern"] = f"{cfg['tag_prefix']}v*"
    cfg["build"] = _merge(BUILD_DEFAULTS, cfg.get("build", {}))
    return cfg


def version_from_tag(cfg: dict, tag: str) -> str:
    """`shop/v4.12.0` → `4.12.0` for an app with tag_prefix `shop/`. Raises on mismatch."""
    m = re.fullmatch(re.escape(cfg["tag_prefix"]) + r"v(\d+\.\d+\.\d+)", tag)
    if not m:
        raise ConfigError(f"'{tag}' doesn't match {cfg['tag_prefix']}vX.Y.Z for app '{cfg['app_id']}'")
    return m.group(1)
