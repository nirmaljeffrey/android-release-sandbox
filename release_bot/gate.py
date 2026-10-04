"""Health gate: turn raw signals into OK / NOT_ENOUGH_DATA / HOLD / HALT.

Everything here is pure (no I/O) so thresholds can be unit tested.
"""

from dataclasses import dataclass, field
from enum import IntEnum


class Level(IntEnum):
    OK = 0
    NOT_ENOUGH_DATA = 1
    HOLD = 2
    HALT = 3


ICONS = {Level.OK: "✅", Level.NOT_ENOUGH_DATA: "⏳", Level.HOLD: "⚠️", Level.HALT: "🛑"}


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


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def evaluate_vitals(new: dict | None, prev: dict | None, cfg: dict, min_users: int) -> list[Finding]:
    """`new`/`prev`: {"distinctUsers", "userPerceivedCrashRate", "userPerceivedAnrRate"}."""
    src = "play-vitals"
    if not new:
        return [Finding(src, Level.NOT_ENOUGH_DATA, "no data for this version yet (review pending or vitals lag)")]

    users = new.get("distinctUsers") or 0
    if users < min_users:
        return [Finding(src, Level.NOT_ENOUGH_DATA, f"{int(users)} daily users < {min_users} minimum")]

    findings = []
    checks = [
        ("userPerceivedCrashRate", "user_perceived_crash_rate", "crash_rate_increase_ratio", "crash rate"),
        ("userPerceivedAnrRate", "user_perceived_anr_rate", "anr_rate_increase_ratio", "ANR rate"),
    ]
    for metric, halt_key, hold_key, label in checks:
        value = new.get(metric)
        if value is None:
            continue
        halt_at = cfg["halt"][halt_key]
        if value >= halt_at:
            findings.append(Finding(src, Level.HALT, f"{label} {_pct(value)} ≥ {_pct(halt_at)} (Google threshold)"))
            continue
        baseline = (prev or {}).get(metric)
        ratio_limit = cfg["hold"][hold_key]
        if baseline and value > baseline * ratio_limit:
            findings.append(Finding(
                src, Level.HOLD,
                f"{label} {_pct(value)} is {value / baseline:.2f}× previous ({_pct(baseline)}), limit {ratio_limit}×",
            ))
        else:
            base = f" vs {_pct(baseline)} previous" if baseline else ""
            findings.append(Finding(src, Level.OK, f"{label} {_pct(value)}{base}"))
    return findings


def evaluate_crashlytics(new_issues: list[dict], cfg: dict) -> list[Finding]:
    """`new_issues`: fatal crash groups first seen in this build, already filtered by min users."""
    src = "crashlytics"
    if not new_issues:
        return [Finding(src, Level.OK, "no new fatal crash groups")]
    return [
        Finding(src, Level.HALT, f"new fatal crash in this build: {i['title']} ({i['users']} users)")
        for i in new_issues[:5]
    ]


def evaluate_grafana(alerts: list[dict], cfg: dict) -> list[Finding]:
    """`alerts`: active Alertmanager alerts ({"labels": {...}, "annotations": {...}})."""
    src = "grafana"
    halt = set(cfg.get("halt_severities", []))
    hold = set(cfg.get("hold_severities", []))
    findings = []
    for a in alerts:
        labels = a.get("labels", {})
        severity = labels.get("severity", "")
        name = labels.get("alertname", "unnamed alert")
        summary = a.get("annotations", {}).get("summary", "")
        text = f"firing: {name}" + (f" — {summary}" if summary else "")
        if severity in halt:
            findings.append(Finding(src, Level.HALT, text))
        elif severity in hold:
            findings.append(Finding(src, Level.HOLD, text))
    return findings or [Finding(src, Level.OK, "no matching alerts firing")]
