"""Health gate: turn raw signals into OK / NOT_ENOUGH_DATA / HOLD / HALT.

Everything here is pure (no I/O) so thresholds can be unit tested.
"""

from dataclasses import dataclass, field
from enum import IntEnum


class Level(IntEnum):
    OK = 0
    NOTIFY = 1           # tell people, keep rolling out
    NOT_ENOUGH_DATA = 2
    HOLD = 3
    HALT = 4


ICONS = {Level.OK: "✅", Level.NOTIFY: "🔔", Level.NOT_ENOUGH_DATA: "⏳", Level.HOLD: "⚠️", Level.HALT: "🛑"}


@dataclass
class Finding:
    source: str
    level: Level
    message: str


@dataclass
class Verdict:
    findings: list[Finding] = field(default_factory=list)

    @property
    def level(self) -> Level:
        return max((f.level for f in self.findings), default=Level.OK)

    def scorecard(self) -> str:
        lines = [f"{ICONS[self.level]} *Health: {self.level.name}*"]
        for f in sorted(self.findings, key=lambda f: -f.level):
            lines.append(f"{ICONS[f.level]} `{f.source}` {f.message}")
        return "\n".join(lines)


# Per-source helpers in the older threshold format. The bot itself evaluates
# configurable rules (release_bot/rules.py); these convert and delegate.

def evaluate_vitals(new: dict | None, prev: dict | None, cfg: dict, min_users: int) -> list[Finding]:
    from release_bot import rules
    norm = rules.normalize({"play_vitals": {**cfg, "enabled": True}, "min_distinct_users": min_users})
    return rules.evaluate(norm, {"play_vitals": {"new": new, "prev": prev}})


def evaluate_crashlytics(new_issues: list[dict], cfg: dict) -> list[Finding]:
    from release_bot import rules
    norm = rules.normalize({"crashlytics": {"new_issue_min_users": 1, **cfg, "enabled": True}})
    return rules.evaluate(norm, {"crashlytics": {"new_issues": new_issues}})


def evaluate_grafana(alerts: list[dict], cfg: dict) -> list[Finding]:
    from release_bot import rules
    norm = rules.normalize({"grafana": {**cfg, "enabled": True}})
    return rules.evaluate(norm, {"grafana": {"alerts": alerts}})
