"""Crashlytics BigQuery (streaming) export: new fatal crash groups in a build.

The export only contains crash events — no session counts — so it can't give a
crash-free rate on its own. Rates come from Play Vitals; Crashlytics is the fast
"a brand new crash appeared in this build" detector.
"""

from google.cloud import bigquery

NEW_FATAL_ISSUES = """
WITH this_build AS (
  SELECT issue_id, ANY_VALUE(issue_title) AS title, COUNT(DISTINCT installation_uuid) AS users
  FROM `{table}`
  WHERE is_fatal
    AND application.build_version = @build
    AND event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  GROUP BY issue_id
),
seen_elsewhere AS (
  SELECT DISTINCT issue_id
  FROM `{table}`
  WHERE application.build_version != @build
    AND event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
)
SELECT t.issue_id, t.title, t.users
FROM this_build t LEFT JOIN seen_elsewhere s USING (issue_id)
WHERE s.issue_id IS NULL AND t.users >= @min_users
ORDER BY t.users DESC
LIMIT 10
"""


class Crashlytics:
    def __init__(self, cfg: dict, client=None):
        self.table = f"{cfg['project']}.{cfg['dataset']}.{cfg['table']}"
        self.days = int(cfg.get("lookback_days", 30))
        self.min_users = int(cfg["new_issue_min_users"])
        self._client = client
        self._project = cfg["project"]

    @property
    def client(self):
        if self._client is None:
            self._client = bigquery.Client(project=self._project)
        return self._client

    def new_fatal_issues(self, version_code: int) -> list[dict]:
        job = self.client.query(
            NEW_FATAL_ISSUES.format(table=self.table),
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("build", "STRING", str(version_code)),
                bigquery.ScalarQueryParameter("days", "INT64", self.days),
                bigquery.ScalarQueryParameter("min_users", "INT64", self.min_users),
            ]),
        )
        return [{"issue_id": r.issue_id, "title": r.title, "users": r.users} for r in job.result()]
