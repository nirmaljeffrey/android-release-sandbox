"""Mock mode (RELEASE_BOT_MOCK=true) for a sandbox repo.

Replaces Google Play, Play Vitals, Crashlytics and Grafana with one JSON state
file. Workflows carry that file between runs with the Actions cache, so the
real workflows, schedules and Slack posts can be exercised end to end without
a Play app. Incidents are injected with `release_bot mock-inject`.
"""

import copy
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from release_bot.play import TrackState


INITIAL = {
    "releases": [{"name": "1.0.0", "versionCodes": ["10000"], "status": "completed"}],
    "history": [],          # [iso_time, version_code, status, fraction]
    "approved_at": None,    # when mock Google review finishes for the live release
    "incident": "none",
}

# Health data each preset produces for the release under rollout.
INCIDENTS = {
    "none": {},
    "new-crash": {"crashlytics": [{"title": "NullPointerException in CheckoutViewModel.onPayClicked", "users": 140}]},
    "anr-regression": {"vitals": {"userPerceivedAnrRate": 0.0030}},     # 1.5× baseline, below Google's line
    "anr-over-threshold": {"vitals": {"userPerceivedAnrRate": 0.0052}},  # over Google's 0.47%
    "grafana-critical": {"grafana": [{
        "labels": {"alertname": "Android API 5xx > 2%", "severity": "critical", "team": "mobile"},
        "annotations": {"summary": "5xx rate 3.4% on /v2/checkout for the new app version"}}]},
    "grafana-warning": {"grafana": [{
        "labels": {"alertname": "Android p95 latency > 1.5s", "severity": "warning", "team": "mobile"},
        "annotations": {"summary": "p95 latency 1.8s on /v2/feed"}}]},
}

BASELINE = {"userPerceivedCrashRate": 0.0040, "userPerceivedAnrRate": 0.0020}


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def state_file() -> Path:
    return Path(os.environ.get("MOCK_STATE_FILE") or ".mock-state/state.json")


def _now() -> datetime:
    from release_bot import cli  # the bot's clock, so tests can control it
    return cli.utcnow()


def version_code_for(version_name: str) -> int:
    """4.12.0 → 41200 (what a real build would usually encode)."""
    parts = [int(p) for p in version_name.split(".")[:3]] + [0, 0]
    return parts[0] * 10000 + parts[1] * 100 + parts[2]


class MockStore:
    def __init__(self, path: Path | None = None):
        self.path = path or state_file()
        self.data = json.loads(self.path.read_text()) if self.path.exists() else copy.deepcopy(INITIAL)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    @property
    def track(self) -> TrackState:
        return TrackState(copy.deepcopy(self.data["releases"]))

    def live_code(self) -> int | None:
        return TrackState.version_code(self.track.live)

    def fraction_now(self) -> float | None:
        approved = self.data.get("approved_at")
        live = self.track.live
        if not live or not approved or _now() < datetime.fromisoformat(approved):
            return None
        return live.get("userFraction", 0)


class MockPlay:
    def __init__(self, store: MockStore, dry_run: bool = False):
        self.store = store
        self.dry_run = dry_run

    def track_state(self) -> TrackState:
        return self.store.track

    def upload_and_start(self, aab_path, version_name, fraction, notes, language) -> int:
        code = version_code_for(version_name)
        if self.dry_run:
            print(f"[dry-run] would upload {version_name} ({code}) at {fraction:.0%}")
            return code
        d = self.store.data
        d["releases"] = [r for r in d["releases"] if r["status"] == "completed"]
        d["releases"].append({
            "name": version_name, "versionCodes": [str(code)], "status": "inProgress",
            "userFraction": fraction, "releaseNotes": [{"language": language, "text": notes}],
        })
        d["approved_at"] = (_now() + timedelta(minutes=_env_int("MOCK_REVIEW_MINUTES", 5))).isoformat()
        d["incident"] = "none"
        self._record(code, "inProgress", fraction)
        print(f"[mock play] uploaded {version_name} ({code}); mock Google review ends {d['approved_at']}")
        return code

    def set_fraction(self, fraction: float) -> dict:
        def mutate(r):
            if fraction >= 1.0:
                r["status"] = "completed"
                r.pop("userFraction", None)
            else:
                r["userFraction"] = fraction
        return self._update(mutate, ("inProgress",), completes=fraction >= 1.0)

    def halt(self) -> dict:
        return self._update(lambda r: r.update(status="halted"), ("inProgress",))

    def resume(self) -> dict:
        return self._update(lambda r: r.update(status="inProgress"), ("halted",))

    def _update(self, mutate, allowed, completes=False) -> dict:
        d = self.store.data
        live = next((r for r in d["releases"] if r["status"] in ("inProgress", "halted")), None)
        if not live or live["status"] not in allowed:
            raise RuntimeError(f"no {'/'.join(allowed)} release on production (current: {live['status'] if live else 'none'})")
        if self.dry_run:
            preview = copy.deepcopy(live)
            mutate(preview)
            print(f"[dry-run] would set {preview}")
            return preview
        mutate(live)
        if completes:
            d["releases"] = [live]
        self._record(int(live["versionCodes"][0]), live["status"], live.get("userFraction", 1.0))
        return copy.deepcopy(live)

    def _record(self, code, status, fraction):
        self.store.data["history"].append([_now().isoformat(), code, status, fraction])


class MockVitals:
    def __init__(self, store: MockStore):
        self.store = store

    def latest_by_version(self) -> dict[int, dict]:
        out = {}
        daily = _env_int("MOCK_DAILY_USERS", 100000)
        prev = TrackState.version_code(self.store.track.completed)
        frac = self.store.fraction_now()
        if prev:
            out[prev] = {"distinctUsers": daily * (1 - (frac or 0)), **BASELINE}
        code = self.store.live_code()
        if code and frac:
            overrides = INCIDENTS[self.store.data.get("incident", "none")].get("vitals", {})
            out[code] = {
                "distinctUsers": daily * frac,
                "userPerceivedCrashRate": 0.0041,
                "userPerceivedAnrRate": 0.0021,
                **overrides,
            }
        return out


class MockCrashlytics:
    def __init__(self, store: MockStore):
        self.store = store

    def new_fatal_issues(self, version_code: int) -> list[dict]:
        if not self.store.fraction_now():
            return []
        issues = INCIDENTS[self.store.data.get("incident", "none")].get("crashlytics", [])
        return [{"issue_id": f"mock-{i}", **x} for i, x in enumerate(issues)]


class MockGrafana:
    def __init__(self, store: MockStore):
        self.store = store

    def active_alerts(self) -> list[dict]:
        return copy.deepcopy(INCIDENTS[self.store.data.get("incident", "none")].get("grafana", []))


def on_duty_logins() -> set[str]:
    """Sandbox stand-in for @android-release-hero (Slack user groups need a paid plan)."""
    return {x.strip() for x in os.environ.get("MOCK_ON_DUTY", "").split(",") if x.strip()}
