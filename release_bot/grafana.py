"""Grafana-managed alerts via the built-in Alertmanager API."""

import requests


class Grafana:
    def __init__(self, url: str, token: str, matchers: list[str], session=None):
        self.url = url.rstrip("/")
        self.token = token
        self.matchers = matchers
        self.http = session or requests.Session()

    def active_alerts(self) -> list[dict]:
        resp = self.http.get(
            f"{self.url}/api/alertmanager/grafana/api/v2/alerts",
            headers={"Authorization": f"Bearer {self.token}"},
            params={"active": "true", "silenced": "false", "inhibited": "false", "filter": self.matchers},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
