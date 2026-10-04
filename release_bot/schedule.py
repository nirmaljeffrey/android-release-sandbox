"""Pure rollout-step logic (no I/O) so it can be unit tested."""

from datetime import datetime
from zoneinfo import ZoneInfo

EPSILON = 1e-9


def target_for(now: datetime, targets: dict[str, float], tz: str) -> float | None:
    """Highest fraction allowed today, or None if today isn't a rollout day."""
    weekday = now.astimezone(ZoneInfo(tz)).strftime("%A").lower()
    return targets.get(weekday)


def next_fraction(current: float, steps: list[float], target: float | None) -> float | None:
    """The next step above `current`, capped by today's target. One step per run."""
    if target is None or current + EPSILON >= target:
        return None
    higher = sorted(s for s in steps if s > current + EPSILON)
    if not higher:
        return None
    return min(higher[0], target)
