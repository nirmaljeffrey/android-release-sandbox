"""Health rules: which signal to watch, what threshold, and what to do.

    health:
      min_users: 1000                     # Play Vitals below this → not enough data yet
      sources:                            # where signals come from (omit a source to turn it off)
        play_vitals: {}
        crashlytics: {project: …, dataset: firebase_crashlytics, table: …, lookback_days: 30}
        grafana: {matchers: ['team="mobile"']}
      rules:
        - {name: Google ANR line, source: play_vitals, metric: user_perceived_anr_rate, above: "0.47%", action: halt}
        - {name: ANR regression,  source: play_vitals, metric: user_perceived_anr_rate, above_previous_by: "25%", action: hold}
        - {name: New crash,       source: crashlytics, metric: new_fatal_issue_users, at_least: 25, action: halt}
        - {name: Pager alert,     source: grafana, severity: critical, action: halt}
        - {name: Latency,         source: grafana, severity: warning, alert: "latency", action: notify}

Actions: halt (stop the rollout), hold (don't advance, ask a human), notify
(tell the channel, keep going). The older per-source threshold format is still
read and converted to rules.

Everything here is pure (no I/O).
"""

import re
from dataclasses import dataclass

from release_bot.gate import Finding, Level
from release_bot.policy import PolicyError, parse_percent

ACTIONS = {"halt": Level.HALT, "hold": Level.HOLD, "notify": Level.NOTIFY}
SOURCES = ("play_vitals", "crashlytics", "grafana")
VITALS_METRICS = {   # rule name → Play Developer Reporting API metric
    "user_perceived_crash_rate": "userPerceivedCrashRate",
    "user_perceived_anr_rate": "userPerceivedAnrRate",
    "crash_rate": "crashRate",
    "anr_rate": "anrRate",
}
VITALS_LABELS = {
    "userPerceivedCrashRate": "crash rate",
    "userPerceivedAnrRate": "ANR rate",
    "crashRate": "crash rate (all)",
    "anrRate": "ANR rate (all)",
}
CRASHLYTICS_METRICS = ("new_fatal_issue_users",)


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    name: str
    source: str
    action: str
    metric: str | None = None
    above: float | None = None              # absolute rate (fraction)
    above_previous_by: float | None = None  # relative: 0.25 = 25% worse than previous version
    at_least: int | None = None             # count (crashlytics users)
    severity: tuple[str, ...] = ()
    alert: str | None = None                # regex on Grafana alertname

    @property
    def level(self) -> Level:
        return ACTIONS[self.action]


def _rate(value, what):
    try:
        return parse_percent(value, what) if isinstance(value, str) else float(value)
    except PolicyError as e:
        raise RuleError(str(e)) from None


def parse_rule(raw: dict, i: int) -> Rule:
    what = f"health.rules[{i}]" + (f" ({raw.get('name')})" if isinstance(raw, dict) and raw.get("name") else "")
    if not isinstance(raw, dict):
        raise RuleError(f"{what}: must be a mapping")
    source, action = raw.get("source"), raw.get("action", "halt")
    if source not in SOURCES:
        raise RuleError(f"{what}: source must be one of {', '.join(SOURCES)}")
    if action not in ACTIONS:
        raise RuleError(f"{what}: action must be halt, hold or notify")
    rule = Rule(name=raw.get("name") or f"rule {i}", source=source, action=action, metric=raw.get("metric"))

    if source == "play_vitals":
        if rule.metric not in VITALS_METRICS:
            raise RuleError(f"{what}: metric must be one of {', '.join(VITALS_METRICS)}")
        if ("above" in raw) == ("above_previous_by" in raw):
            raise RuleError(f"{what}: set exactly one of `above` (e.g. '0.47%') or `above_previous_by` (e.g. '25%')")
        if "above" in raw:
            rule.above = _rate(raw["above"], what)
        else:
            rule.above_previous_by = _rate(raw["above_previous_by"], what)
    elif source == "crashlytics":
        rule.metric = rule.metric or "new_fatal_issue_users"
        if rule.metric not in CRASHLYTICS_METRICS:
            raise RuleError(f"{what}: metric must be one of {', '.join(CRASHLYTICS_METRICS)}")
        rule.at_least = int(raw.get("at_least", 1))
    else:  # grafana
        sev = raw.get("severity")
        if not sev:
            raise RuleError(f"{what}: set `severity` (e.g. critical) for Grafana rules")
        rule.severity = tuple([sev] if isinstance(sev, str) else sev)
        rule.alert = raw.get("alert")
        if rule.alert:
            re.compile(rule.alert)
    return rule


def _legacy_rules(health: dict) -> list[Rule]:
    rules = []
    pv = health.get("play_vitals", {}) or {}
    halt, hold = pv.get("halt", {}) or {}, pv.get("hold", {}) or {}
    for metric, label in (("user_perceived_crash_rate", "crash"), ("user_perceived_anr_rate", "anr")):
        if metric in halt:
            rules.append(Rule("Google threshold", "play_vitals", "halt", metric, above=float(halt[metric])))
        ratio = hold.get(f"{label}_rate_increase_ratio")
        if ratio:
            rules.append(Rule("previous version", "play_vitals", "hold", metric,
                              above_previous_by=float(ratio) - 1))
    cr = health.get("crashlytics", {}) or {}
    rules.append(Rule("new crash", "crashlytics", "halt", "new_fatal_issue_users",
                      at_least=int(cr.get("new_issue_min_users", 25))))
    gf = health.get("grafana", {}) or {}
    for sev in gf.get("halt_severities", ["critical"]):
        rules.append(Rule("alert", "grafana", "halt", severity=(sev,)))
    for sev in gf.get("hold_severities", ["warning"]):
        rules.append(Rule("alert", "grafana", "hold", severity=(sev,)))
    return rules


def normalize(health: dict) -> dict:
    """{'min_users', 'sources': {name: cfg}, 'rules': [Rule]} from either format."""
    health = health or {}
    if "rules" in health or "sources" in health:
        sources = {name: (cfg or {}) for name, cfg in (health.get("sources") or {}).items()
                   if name in SOURCES and (cfg or {}).get("enabled", True)}
        rules = [parse_rule(r, i) for i, r in enumerate(health.get("rules") or [], 1)]
        return {"min_users": int(health.get("min_users", 1000)), "sources": sources, "rules": rules}

    sources = {}
    for name in SOURCES:
        cfg = health.get(name, {}) or {}
        if cfg.get("enabled", False):
            sources[name] = {k: v for k, v in cfg.items() if k not in ("enabled", "halt", "hold")}
    return {"min_users": int(health.get("min_distinct_users", 1000)), "sources": sources,
            "rules": _legacy_rules(health)}


def crashlytics_query_min_users(norm: dict) -> int:
    """Smallest `at_least` among Crashlytics rules, so the query returns every candidate."""
    values = [r.at_least for r in norm["rules"] if r.source == "crashlytics" and r.at_least]
    return min(values) if values else 25


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def evaluate(norm: dict, signals: dict) -> list[Finding]:
    """`signals`: {source: data | Exception} for each source that was queried.
    play_vitals → {"new": {...} | None, "prev": {...} | None}
    crashlytics → {"new_issues": [{"title", "users"}]}
    grafana     → {"alerts": [{"labels", "annotations"}]}
    A source that failed produces HOLD (can't prove it's healthy), never HALT."""
    findings = []
    for source, data in signals.items():
        if isinstance(data, Exception):
            findings.append(Finding(source, Level.HOLD, f"could not fetch: {data}"))
            continue
        rules = [r for r in norm["rules"] if r.source == source]
        if source == "play_vitals":
            findings += _eval_vitals(rules, data, norm["min_users"])
        elif source == "crashlytics":
            findings += _eval_crashlytics(rules, data)
        elif source == "grafana":
            findings += _eval_grafana(rules, data)
    return findings


def _eval_vitals(rules: list[Rule], data: dict, min_users: int) -> list[Finding]:
    src = "play-vitals"
    new, prev = data.get("new"), data.get("prev") or {}
    if not new:
        return [Finding(src, Level.NOT_ENOUGH_DATA, "no data for this version yet (review pending or vitals lag)")]
    users = new.get("distinctUsers") or 0
    if users < min_users:
        return [Finding(src, Level.NOT_ENOUGH_DATA, f"{int(users)} daily users < {min_users} minimum")]

    findings, metrics = [], []
    for r in rules:
        api = VITALS_METRICS[r.metric]
        if api not in metrics:
            metrics.append(api)
    for api in metrics:
        value = new.get(api)
        if value is None:
            continue
        label, base = VITALS_LABELS[api], prev.get(api)
        breached = []
        for r in (r for r in rules if VITALS_METRICS[r.metric] == api):
            if r.above is not None and value >= r.above:
                breached.append(Finding(src, r.level, f"{label} {_pct(value)} ≥ {_pct(r.above)} ({r.name})"))
            elif r.above_previous_by is not None and base and value > base * (1 + r.above_previous_by):
                breached.append(Finding(src, r.level,
                    f"{label} {_pct(value)} is {value / base:.2f}× previous ({_pct(base)}), "
                    f"limit +{r.above_previous_by * 100:g}% ({r.name})"))
        if breached:
            findings.append(max(breached, key=lambda f: f.level))
        else:
            findings.append(Finding(src, Level.OK, f"{label} {_pct(value)}" + (f" vs {_pct(base)} previous" if base else "")))
    return findings


def _eval_crashlytics(rules: list[Rule], data: dict) -> list[Finding]:
    src = "crashlytics"
    issues = data.get("new_issues", [])
    findings = []
    for r in rules:
        for issue in [i for i in issues if i["users"] >= (r.at_least or 1)][:5]:
            findings.append(Finding(src, r.level, f"new fatal crash in this build: {issue['title']} ({issue['users']} users)"))
    # One finding per issue: keep the most severe rule that matched it.
    best: dict[str, Finding] = {}
    for f in findings:
        if f.message not in best or f.level > best[f.message].level:
            best[f.message] = f
    return list(best.values()) or [Finding(src, Level.OK, "no new fatal crash groups")]


def _eval_grafana(rules: list[Rule], data: dict) -> list[Finding]:
    src = "grafana"
    findings = []
    for a in data.get("alerts", []):
        labels = a.get("labels", {})
        name = labels.get("alertname", "unnamed alert")
        matched = [r for r in rules
                   if labels.get("severity", "") in r.severity and (not r.alert or re.search(r.alert, name, re.I))]
        if not matched:
            continue
        rule = max(matched, key=lambda r: r.level)
        summary = a.get("annotations", {}).get("summary", "")
        findings.append(Finding(src, rule.level, f"firing: {name}" + (f" — {summary}" if summary else "")))
    return findings or [Finding(src, Level.OK, "no matching alerts firing")]
