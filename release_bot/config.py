import os
from pathlib import Path

import yaml

DEFAULT_PATH = Path("release-bot.yml")

# Repo variables can override Slack IDs without editing release-bot.yml.
ENV_OVERRIDES = {
    "SLACK_CHANNEL_ID": "channel_id",
    "SLACK_ANNOUNCE_CHANNEL_ID": "announce_channel_id",
    "SLACK_ALERTS_CHANNEL_ID": "alerts_channel_id",
    "SLACK_HERO_USERGROUP_ID": "hero_usergroup_id",
}


def load(path: str | Path = DEFAULT_PATH) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    for env, key in ENV_OVERRIDES.items():
        if os.environ.get(env):
            cfg["slack"][key] = os.environ[env]
    return cfg
