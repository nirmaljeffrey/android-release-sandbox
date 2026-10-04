"""Slack: one thread per release, announcements, and the on-duty release hero.

Stateless: a release's thread is found again by a marker in its root message.
Bot scopes: chat:write, channels:history, channels:join (groups:history for private), usergroups:read.
"""

import requests

API = "https://slack.com/api"


class Slack:
    def __init__(self, token: str | None, channel: str, dry_run: bool = False, session=None):
        self.token = token
        self.channel = channel
        self.dry_run = dry_run
        self.http = session or requests.Session()

    @property
    def _can_post(self) -> bool:
        return bool(self.token) and not self.dry_run

    @staticmethod
    def marker(version: str) -> str:
        return f"[android-release {version}]"

    def post(self, version: str, text: str, root_text: str | None = None) -> None:
        if not self._can_post:
            print(f"[slack not posted] {version}: {text}")
            return
        ts = self._find_thread(version)
        if ts is None:
            root = root_text or f"🤖 Android release *{version}*"
            ts = self._call("chat.postMessage", channel=self.channel,
                            text=f"{root}\n_{self.marker(version)}_")["ts"]
        self._call("chat.postMessage", channel=self.channel, thread_ts=ts, text=text)

    def thread_contains(self, version: str, text: str) -> bool:
        """True if `text` was already posted in this release's thread (dedupe)."""
        if not self.token:
            return False
        ts = self._find_thread(version)
        if ts is None:
            return False
        data = self._call("conversations.replies", get=True, channel=self.channel, ts=ts, limit=200)
        return any(text in m.get("text", "") for m in data.get("messages", []))

    def announce(self, channel: str | None, text: str) -> None:
        """Top-level message in another channel (wider group, SLO alerts)."""
        if not channel:
            return
        if not self._can_post:
            print(f"[slack not posted] #{channel}: {text}")
            return
        self._call("chat.postMessage", channel=channel, text=text)

    def usergroup_members(self, usergroup_id: str) -> set[str]:
        """Slack user IDs currently in a user group, e.g. @android-release-hero."""
        if not self.token:
            raise RuntimeError("SLACK_BOT_TOKEN is required to check the release hero")
        data = self._call("usergroups.users.list", get=True, usergroup=usergroup_id)
        return set(data.get("users", []))

    def _find_thread(self, version: str) -> str | None:
        data = self._call("conversations.history", get=True, channel=self.channel, limit=200)
        for msg in data.get("messages", []):
            if self.marker(version) in msg.get("text", ""):
                return msg["ts"]
        return None

    def _call(self, method: str, get: bool = False, _joined: bool = False, **params) -> dict:
        # Read methods take query params; chat.postMessage takes a JSON body.
        headers = {"Authorization": f"Bearer {self.token}"}
        if get:
            resp = self.http.get(f"{API}/{method}", params=params, headers=headers, timeout=30)
        else:
            resp = self.http.post(f"{API}/{method}", json=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok") and data.get("error") == "not_in_channel" and params.get("channel") and not _joined:
            # Public channel the bot hasn't joined yet: join (channels:join) and retry once.
            self._call("conversations.join", channel=params["channel"])
            return self._call(method, get=get, _joined=True, **params)
        if not data.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {data.get('error')}")
        return data
