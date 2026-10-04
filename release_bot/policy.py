"""Rollout policy: the user-defined schedule, per platform.

    rollout:
      platforms: aligned          # aligned: one schedule for Android and iOS
                                  # separate: android: {schedule: …} and ios: {schedule: …}
      schedule:                   # steps in order; `on` is a weekday or "day N" (N days after submit)
        - {on: monday,    percent: 1}
        - {on: tuesday,   percent: 2}
        - {on: wednesday, percent: 100}
      when_data_is_thin: hold     # hold | advance

The first step is what goes live when the store approves the release. Each
later step becomes allowed on its day; the bot still moves at most one step
per run and only when health allows it.

The older format (`steps` + weekday `targets` + `play.initial_fraction`) is
still read.

Everything here is pure (no I/O).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# Apple's phased release: share of users on each day after it starts. Fixed by Apple.
APPLE_PHASED = [0.01, 0.02, 0.05, 0.10, 0.20, 0.50, 1.0]
EPSILON = 1e-9


class PolicyError(ValueError):
    pass


def parse_percent(value, what: str) -> float:
    """1 → 0.01, "1%" → 0.01, "0.47%" → 0.0047. Plain numbers are percentages."""
    if isinstance(value, str):
        m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*%?\s*", value)
        if not m:
            raise PolicyError(f"{what}: '{value}' is not a percentage")
        value = float(m.group(1))
    if not isinstance(value, (int, float)) or not 0 < value <= 100:
        raise PolicyError(f"{what}: percent must be between 0 and 100, got {value!r}")
    return float(value) / 100


def _parse_on(on, what: str):
    """'monday' → 'monday'; 'day 4' / 4 → 4 (days after the submit day)."""
    if isinstance(on, int) and not isinstance(on, bool):
        if on < 0:
            raise PolicyError(f"{what}: day must be ≥ 0")
        return on
    if isinstance(on, str):
        s = on.strip().lower()
        if s[:3] in [d[:3] for d in WEEKDAYS]:
            return next(d for d in WEEKDAYS if d.startswith(s[:3]))
        m = re.fullmatch(r"day\s*(\d+)", s)
        if m:
            return int(m.group(1))
    raise PolicyError(f"{what}: `on` must be a weekday (monday) or 'day N', got {on!r}")


def _pct(f: float) -> str:
    return f"{f * 100:g}%"


@dataclass
class Step:
    on: object            # weekday name, int day offset, or None (legacy "at submit")
    fraction: float

    def label(self) -> str:
        if self.on is None:
            return "on approval"
        return self.on.title()[:3] if isinstance(self.on, str) else f"day {self.on}"


@dataclass
class Schedule:
    steps: list[Step]
    # Legacy configs may list ladder rungs that no weekday targets directly.
    ladder_override: list[float] | None = None

    @property
    def kind(self) -> str:
        kinds = {"weekday" if isinstance(s.on, str) else "day" for s in self.steps if s.on is not None}
        return kinds.pop() if kinds else "weekday"

    @property
    def initial(self) -> float:
        return self.steps[0].fraction

    @property
    def ladder(self) -> list[float]:
        return self.ladder_override or [s.fraction for s in self.steps]

    def target(self, now: datetime, tz: str, submitted_at: datetime | None = None) -> float | None:
        """Highest fraction allowed right now, or None if no step is due today."""
        local = now.astimezone(ZoneInfo(tz))
        if self.kind == "weekday":
            today = WEEKDAYS[local.weekday()]
            due = [s.fraction for s in self.steps if s.on == today]
            return max(due) if due else None
        if submitted_at is None:
            return None
        days = (local.date() - submitted_at.astimezone(ZoneInfo(tz)).date()).days
        due = [s.fraction for s in self.steps if isinstance(s.on, int) and s.on <= days]
        return max(due) if due else None

    def next_fraction(self, current: float, target: float | None) -> float | None:
        """The rung above `current`, never beyond today's target. One step per run."""
        if target is None or current + EPSILON >= target:
            return None
        higher = sorted(f for f in self.ladder if f > current + EPSILON)
        return min(higher[0], target) if higher else None

    def summary(self) -> str:
        return " → ".join(f"{_pct(s.fraction)} {s.label()}" for s in self.steps)

    def day_offsets(self) -> list[int]:
        """Days from the first step to each step (weekday schedules wrap forward)."""
        if self.kind == "day":
            first = next((s.on for s in self.steps if isinstance(s.on, int)), 0)
            return [(s.on - first) if isinstance(s.on, int) else 0 for s in self.steps]
        offsets, total, prev = [], 0, None
        for s in self.steps:
            if s.on is None:
                offsets.append(0)
                continue
            if prev is not None:
                gap = (WEEKDAYS.index(s.on) - WEEKDAYS.index(prev)) % 7 or 7
                total += gap
            offsets.append(total)
            prev = s.on
        return offsets


@dataclass
class RolloutPolicy:
    mode: str
    android: Schedule
    ios: Schedule | None
    when_data_is_thin: str = "hold"
    warnings: list[str] = field(default_factory=list)


def _schedule_from(steps_cfg, what: str) -> Schedule:
    if not steps_cfg:
        raise PolicyError(f"{what}: schedule needs at least one step")
    steps = []
    for i, raw in enumerate(steps_cfg, 1):
        # YAML 1.1 reads a bare `on:` key as the boolean True; accept both spellings.
        on = raw.get("on", raw.get(True)) if isinstance(raw, dict) else None
        if on is None or "percent" not in raw:
            raise PolicyError(f"{what} step {i}: needs `on` and `percent`")
        steps.append(Step(_parse_on(on, f"{what} step {i}"), parse_percent(raw["percent"], f"{what} step {i}")))
    kinds = {"weekday" if isinstance(s.on, str) else "day" for s in steps}
    if len(kinds) > 1:
        raise PolicyError(f"{what}: use either weekdays or 'day N' for every step, not both")
    sched = Schedule(steps)
    if sched.kind == "weekday":
        days = [s.on for s in steps]
        if len(set(days)) != len(days):
            raise PolicyError(f"{what}: each weekday can appear once; use 'day N' for longer rollouts")
        if sched.day_offsets()[-1] > 6:
            raise PolicyError(f"{what}: a weekday schedule must fit within one week "
                              f"({' → '.join(d[:3].title() for d in days)} spans {sched.day_offsets()[-1]} days); "
                              "use 'day N' steps for longer rollouts")
    for a, b in zip(steps, steps[1:]):
        if b.fraction <= a.fraction:
            raise PolicyError(f"{what}: percentages must increase ({_pct(a.fraction)} then {_pct(b.fraction)})")
        if isinstance(a.on, int) and b.on <= a.on:
            raise PolicyError(f"{what}: days must increase (day {a.on} then day {b.on})")
    return Schedule(steps)


def _legacy_schedule(rollout: dict, play: dict) -> Schedule:
    ladder = [float(x) for x in rollout.get("steps", [0.02, 0.20, 0.50, 1.0])]
    initial = float(play.get("initial_fraction", ladder[0]))
    targets = rollout.get("targets", {})
    steps = [Step(None, initial)] + [
        Step(day, float(f)) for day, f in sorted(targets.items(), key=lambda kv: (kv[1], WEEKDAYS.index(kv[0])))
    ]
    return Schedule(steps, ladder_override=sorted(set(ladder) | {initial}))


def from_config(cfg: dict) -> RolloutPolicy:
    """Build the policy for one resolved app config (see config.resolve)."""
    rollout = cfg.get("rollout", {}) or {}
    thin = rollout.get("when_data_is_thin", "hold")
    if thin not in ("hold", "advance"):
        raise PolicyError("rollout.when_data_is_thin must be 'hold' or 'advance'")

    if "schedule" not in rollout and "android" not in rollout and "ios" not in rollout:
        return RolloutPolicy("aligned", _legacy_schedule(rollout, cfg.get("play", {})), None, thin)

    mode = rollout.get("platforms", "aligned")
    if mode == "aligned":
        shared = _schedule_from(rollout.get("schedule"), "rollout.schedule")
        android, ios = shared, shared
    elif mode == "separate":
        android = _schedule_from((rollout.get("android") or {}).get("schedule"), "rollout.android.schedule")
        ios_cfg = (rollout.get("ios") or {}).get("schedule")
        ios = _schedule_from(ios_cfg, "rollout.ios.schedule") if ios_cfg else None
    else:
        raise PolicyError("rollout.platforms must be 'aligned' or 'separate'")
    return RolloutPolicy(mode, android, ios, thin)


def check(policy: RolloutPolicy, platforms: list[str]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the platforms an app ships on."""
    errors, warnings = [], []
    if "android" in platforms:
        first = policy.android.steps[0]
        # Approval usually lands within a day, so only weekday or day ≥ 2 starts are misleading.
        if first.on is not None and not (isinstance(first.on, int) and first.on <= 1):
            warnings.append(
                f"Android: {_pct(first.fraction)} goes live as soon as Google approves the release. "
                f"Play can't hold an approved release until {first.label()} yet, so expect it earlier.")
    if "ios" in platforms:
        if policy.ios is None:
            errors.append("iOS: no schedule. Add rollout.ios.schedule or use platforms: aligned")
        else:
            errors += _check_ios(policy.ios, policy.mode)
            if policy.ios.steps[-1].fraction < 1.0:
                warnings.append("iOS: the last step is below 100%; Apple finishes the phased release "
                                "by itself on day 7.")
    return errors, warnings


def _check_ios(schedule: Schedule, mode: str) -> list[str]:
    """Apple's phased release can only follow APPLE_PHASED, be paused, or jump to 100%."""
    errors = []
    hint = " Or set rollout.platforms: separate and give iOS its own schedule." if mode == "aligned" else ""
    for step, offset in zip(schedule.steps, schedule.day_offsets()):
        if step.fraction >= 1.0:
            continue  # "release to everyone" is allowed any day
        day = offset + 1
        if day > len(APPLE_PHASED):
            errors.append(f"iOS: {_pct(step.fraction)} on {step.label()} is phased-release day {day}; "
                          f"Apple is already at 100% by then.{hint}")
            continue
        expected = APPLE_PHASED[day - 1]
        if abs(step.fraction - expected) > EPSILON:
            errors.append(f"iOS: {_pct(step.fraction)} on {step.label()} is phased-release day {day}, "
                          f"where Apple is at {_pct(expected)}. Use {_pct(expected)} or 100%.{hint}")
    return errors


def plan_rows(policy: RolloutPolicy, platforms: list[str]) -> list[dict]:
    """Day-by-day view per platform, for `release_bot plan`."""
    rows = []
    if "android" in platforms:
        for i, s in enumerate(policy.android.steps):
            how = "goes live when Google approves" if i == 0 else "bot raises it, if healthy"
            rows.append({"platform": "Android", "when": s.label(), "percent": _pct(s.fraction), "how": how})
    if "ios" in platforms and policy.ios:
        for i, (s, off) in enumerate(zip(policy.ios.steps, policy.ios.day_offsets())):
            if i == 0:
                how = "bot starts Apple's phased release (day 1)"
            elif s.fraction >= 1.0:
                how = "bot releases to everyone, if healthy"
            else:
                how = f"Apple's phased release, day {off + 1} (bot pauses if unhealthy)"
            rows.append({"platform": "iOS", "when": s.label(), "percent": _pct(s.fraction), "how": how})
    return rows
