"""Play Developer Reporting API: user-perceived crash and ANR rates per versionCode.

Daily data, typically 1–2 days behind. Rates are fractions (0.0109 == 1.09%).
"""

from datetime import date, timedelta

import google.auth
from googleapiclient.discovery import build

SCOPE = "https://www.googleapis.com/auth/playdeveloperreporting"
PLAY_TZ = {"id": "America/Los_Angeles"}  # required by the API for DAILY aggregation


def _service():
    creds, _ = google.auth.default(scopes=[SCOPE])
    return build("playdeveloperreporting", "v1beta1", credentials=creds, cache_discovery=False)


def _gdate(d: date) -> dict:
    return {"year": d.year, "month": d.month, "day": d.day, "timeZone": PLAY_TZ}


def _metric_value(m: dict) -> float | None:
    v = m.get("decimalValue", {}).get("value")
    return float(v) if v is not None else None


class Vitals:
    def __init__(self, package: str, service=None):
        self.package = package
        self._svc = service

    @property
    def svc(self):
        if self._svc is None:
            self._svc = _service()
        return self._svc

    def latest_by_version(self, days: int = 3) -> dict[int, dict]:
        """{versionCode: {"distinctUsers", "userPerceivedCrashRate", "userPerceivedAnrRate"}} for the freshest day."""
        out: dict[int, dict] = {}
        sets = [
            (self.svc.vitals().crashrate(), "crashRateMetricSet", ["userPerceivedCrashRate", "distinctUsers"]),
            (self.svc.vitals().anrrate(), "anrRateMetricSet", ["userPerceivedAnrRate"]),
        ]
        for resource, set_name, metrics in sets:
            name = f"apps/{self.package}/{set_name}"
            end = self._latest_daily_end(resource, name)
            body = {
                "timelineSpec": {
                    "aggregationPeriod": "DAILY",
                    "startTime": _gdate(end - timedelta(days=days)),
                    "endTime": _gdate(end),
                },
                "dimensions": ["versionCode"],
                "metrics": metrics,
                "pageSize": 1000,
            }
            rows = resource.query(name=name, body=body).execute().get("rows", [])
            latest_day: dict[int, tuple] = {}
            for row in rows:
                dim = row["dimensions"][0]
                code = int(dim.get("stringValue") or dim.get("int64Value"))
                st = row["startTime"]
                day = (st["year"], st["month"], st["day"])
                if code in latest_day and latest_day[code] > day:
                    continue
                latest_day[code] = day
                values = out.setdefault(code, {})
                for m in row.get("metrics", []):
                    values[m["metric"]] = _metric_value(m)
        return out

    @staticmethod
    def _latest_daily_end(resource, name: str) -> date:
        info = resource.get(name=name).execute().get("freshnessInfo", {})
        for f in info.get("freshnesses", []):
            if f.get("aggregationPeriod") == "DAILY":
                t = f["latestEndTime"]
                return date(t["year"], t["month"], t["day"])
        return date.today() - timedelta(days=2)
