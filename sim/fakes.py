"""Mock Play, Vitals, Crashlytics, Grafana and Slack for the simulator.

They expose the same methods the real clients do, so release_bot runs unchanged.
"""

import copy
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from release_bot.play import TrackState
from release_bot.slack import Slack


class Clock:
    def __init__(self, now: datetime):
        self.now = now


# ---------------------------------------------------------------- Play

class FakePlay:
    """Production track kept in memory. Records every fraction change so mock
    Vitals can report what was live N hours ago (Vitals lag)."""

    def __init__(self, clock: Clock, previous: dict, new_version_code: int, review_hours: float):
        self.clock = clock
        self.new_code = new_version_code
        self.review_hours = review_hours
        self.approved_at: datetime | None = None
        self.releases = [{
            "name": previous["name"],
            "versionCodes": [str(previous["version_code"])],
            "status": previous.get("status", "completed"),
            **({"userFraction": previous["fraction"]} if "fraction" in previous else {}),
        }]
        self.history: list[tuple[datetime, int, str, float]] = []
        self.log: list[str] = []

    # --- same interface as release_bot.play.Play
    def track_state(self) -> TrackState:
        return TrackState(copy.deepcopy(self.releases))

    def upload_and_start(self, aab, version_name, fraction, notes, language) -> int:
        self.releases = [r for r in self.releases if r["status"] == "completed"]
        self.releases.append({
            "name": version_name, "versionCodes": [str(self.new_code)], "status": "inProgress",
            "userFraction": fraction, "releaseNotes": [{"language": language, "text": notes}],
        })
        self.approved_at = self.clock.now + timedelta(hours=self.review_hours)
        self._record("inProgress", fraction)
        self.log.append(f"uploaded {version_name} ({self.new_code}) at {fraction:.0%}, in Google review")
        return self.new_code

    def set_fraction(self, fraction: float) -> dict:
        live = self._live(("inProgress",))
        if fraction >= 1.0:
            self.releases = [live]
            live["status"] = "completed"
            live.pop("userFraction", None)
            self._record("completed", 1.0)
        else:
            live["userFraction"] = fraction
            self._record("inProgress", fraction)
        self.log.append(f"rollout → {fraction:.0%}")
        return copy.deepcopy(live)

    def halt(self) -> dict:
        live = self._live(("inProgress",))
        live["status"] = "halted"
        self._record("halted", live.get("userFraction", 0))
        self.log.append(f"HALTED at {live.get('userFraction', 0):.0%}")
        return copy.deepcopy(live)

    def resume(self) -> dict:
        live = self._live(("halted",))
        live["status"] = "inProgress"
        self._record("inProgress", live.get("userFraction", 0))
        self.log.append(f"resumed at {live.get('userFraction', 0):.0%}")
        return copy.deepcopy(live)

    # --- simulator helpers
    def is_live(self, at: datetime) -> bool:
        return self.approved_at is not None and at >= self.approved_at

    def fraction_at(self, at: datetime) -> float | None:
        """Share of users on the new version at time `at` (None before approval)."""
        if not self.is_live(at):
            return None
        frac = None
        for t, _, _, f in self.history:
            if t <= at:
                frac = f
        return frac

    def _live(self, allowed):
        live = TrackState(self.releases).live
        if not live or live["status"] not in allowed:
            raise RuntimeError(f"no {'/'.join(allowed)} release on production")
        return live

    def _record(self, status, fraction):
        self.history.append((self.clock.now, self.new_code, status, fraction))


# ---------------------------------------------------------------- health sources

def _active(items: list[dict], now: datetime, parse) -> list[dict]:
    out = []
    for item in items:
        start = parse(item["from"])
        end = parse(item["until"]) if item.get("until") else None
        if start <= now and (end is None or now < end):
            out.append(item)
    return out


def _value_at(points: list[dict], at: datetime, parse, key: str, default: float) -> float:
    value = default
    for p in sorted(points, key=lambda p: parse(p["from"])):
        if parse(p["from"]) <= at and key in p:
            value = p[key]
    return value


class FakeVitals:
    def __init__(self, clock: Clock, play: FakePlay, scenario: dict, parse):
        self.clock, self.play, self.s, self.parse = clock, play, scenario, parse

    def latest_by_version(self) -> dict[int, dict]:
        lagged = self.clock.now - timedelta(hours=self.s.get("vitals_lag_hours", 36))
        daily = self.s["daily_users"]
        base = self.s["baseline"]
        prev_code = int(TrackState(self.play.releases).completed["versionCodes"][0]) \
            if TrackState(self.play.releases).completed else self.s["previous_release"]["version_code"]
        frac = self.play.fraction_at(lagged)
        out = {prev_code: {
            "distinctUsers": daily * (1 - (frac or 0)),
            "userPerceivedCrashRate": base["crash"],
            "userPerceivedAnrRate": base["anr"],
        }}
        if frac:
            points = self.s.get("new_version", [])
            out[self.play.new_code] = {
                "distinctUsers": daily * frac,
                "userPerceivedCrashRate": _value_at(points, lagged, self.parse, "crash", base["crash"]),
                "userPerceivedAnrRate": _value_at(points, lagged, self.parse, "anr", base["anr"]),
            }
        return out


class FakeCrashlytics:
    def __init__(self, clock: Clock, play: FakePlay, scenario: dict, parse, min_users: int):
        self.clock, self.play, self.s, self.parse, self.min_users = clock, play, scenario, parse, min_users

    def new_fatal_issues(self, version_code: int) -> list[dict]:
        if not self.play.is_live(self.clock.now):
            return []
        issues = _active(self.s.get("crashlytics_new_issues", []), self.clock.now, self.parse)
        return [{"issue_id": f"sim-{i}", "title": x["title"], "users": x["users"]}
                for i, x in enumerate(issues) if x["users"] >= self.min_users]


class FakeGrafana:
    def __init__(self, clock: Clock, scenario: dict, parse):
        self.clock, self.s, self.parse = clock, scenario, parse

    def active_alerts(self) -> list[dict]:
        return [{"labels": a["labels"], "annotations": a.get("annotations", {})}
                for a in _active(self.s.get("grafana_alerts", []), self.clock.now, self.parse)]


# ---------------------------------------------------------------- Slack

@dataclass
class SlackEntry:
    at: datetime
    channel: str          # "release" | "announce" | "alerts"
    text: str
    thread: str | None = None   # version, for release-thread replies
    root: bool = False


@dataclass
class RecordingSlack:
    """Records every message for the HTML transcript; optionally mirrors to a
    real Slack test channel. The on-duty check always uses the scenario."""
    clock: Clock
    cfg_slack: dict
    on_duty: set[str]
    real: dict | None = None      # {"token", "release", "announce", "alerts", "run_id"}
    entries: list[SlackEntry] = field(default_factory=list)
    _threads: set = field(default_factory=set)

    def post(self, version: str, text: str, root_text: str | None = None) -> None:
        if version not in self._threads:
            self._threads.add(version)
            self.entries.append(SlackEntry(self.clock.now, "release",
                                           root_text or f"🤖 Android release *{version}*", version, root=True))
        self.entries.append(SlackEntry(self.clock.now, "release", text, version))
        self._mirror_thread(version, text, root_text)

    def announce(self, channel: str | None, text: str) -> None:
        if not channel:
            return
        kind = "announce" if channel == self.cfg_slack.get("announce_channel_id") else "alerts"
        self.entries.append(SlackEntry(self.clock.now, kind, text))
        self._mirror_channel(kind, text)

    def usergroup_members(self, usergroup_id: str) -> set[str]:
        return set(self.on_duty)

    def thread_contains(self, version: str, text: str) -> bool:
        return any(e.thread == version and text in e.text for e in self.entries)

    # --- real Slack mirroring
    def _stamp(self, text: str) -> str:
        return f"`sim {self.clock.now:%a %H:%M} UTC` {text}"

    def _client(self, kind: str) -> Slack:
        return Slack(self.real["token"], self.real.get(kind) or self.real["release"])

    def _mirror_thread(self, version, text, root_text):
        if not self.real:
            return
        tag = f"{version} · sim {self.real['run_id']}"
        self._client("release").post(tag, self._stamp(text),
                                     root_text=self._stamp(root_text) if root_text else None)
        time.sleep(1.1)  # stay under chat.postMessage rate limits

    def _mirror_channel(self, kind, text):
        if not self.real:
            return
        client = self._client(kind)
        client.announce(client.channel, f"[{kind}] {self._stamp(text)}")
        time.sleep(1.1)
