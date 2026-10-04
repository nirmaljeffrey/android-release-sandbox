"""Google Play Publishing API (androidpublisher v3): upload, rollout, halt, resume.

Auth comes from Application Default Credentials, which google-github-actions/auth
sets up via Workload Identity Federation — no JSON key file.
"""

from dataclasses import dataclass

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPE = "https://www.googleapis.com/auth/androidpublisher"
LIVE_STATUSES = ("inProgress", "halted")


def _service():
    creds, _ = google.auth.default(scopes=[SCOPE])
    return build("androidpublisher", "v3", credentials=creds, cache_discovery=False)


@dataclass
class TrackState:
    releases: list[dict]

    @property
    def live(self) -> dict | None:
        """The staged (inProgress) or halted release, if a rollout is underway."""
        return next((r for r in self.releases if r.get("status") in LIVE_STATUSES), None)

    @property
    def completed(self) -> dict | None:
        """The release currently serving everyone else (the previous version)."""
        done = [r for r in self.releases if r.get("status") == "completed"]
        return max(done, key=lambda r: max(int(v) for v in r["versionCodes"]), default=None)

    @staticmethod
    def version_code(release: dict | None) -> int | None:
        if not release or not release.get("versionCodes"):
            return None
        return max(int(v) for v in release["versionCodes"])


class Play:
    def __init__(self, package: str, track: str, changes_not_sent_for_review: bool = False,
                 dry_run: bool = False, service=None):
        self.package = package
        self.track = track
        self.skip_review = changes_not_sent_for_review
        self.dry_run = dry_run
        self._svc = service

    @property
    def edits(self):
        if self._svc is None:
            self._svc = _service()
        return self._svc.edits()

    def track_state(self) -> TrackState:
        edit = self.edits.insert(packageName=self.package, body={}).execute()
        try:
            track = self.edits.tracks().get(packageName=self.package, editId=edit["id"], track=self.track).execute()
        finally:
            self.edits.delete(packageName=self.package, editId=edit["id"]).execute()
        return TrackState(track.get("releases", []))

    def upload_and_start(self, aab_path: str, version_name: str, fraction: float,
                         notes: str, language: str) -> int:
        if self.dry_run:
            print(f"[dry-run] would upload {aab_path} as {version_name} at {fraction:.0%}")
            return -1
        edit = self.edits.insert(packageName=self.package, body={}).execute()
        media = MediaFileUpload(aab_path, mimetype="application/octet-stream", resumable=True)
        bundle = self.edits.bundles().upload(
            packageName=self.package, editId=edit["id"], media_body=media).execute()
        version_code = int(bundle["versionCode"])
        release = {
            "name": version_name,
            "versionCodes": [str(version_code)],
            "status": "inProgress",
            "userFraction": fraction,
            "releaseNotes": [{"language": language, "text": notes[:500]}],
        }
        self._commit_track(edit["id"], release)
        return version_code

    def set_fraction(self, fraction: float) -> dict:
        def mutate(r):
            if fraction >= 1.0:
                r["status"] = "completed"
                r.pop("userFraction", None)
            else:
                r["status"] = "inProgress"
                r["userFraction"] = fraction
        return self._update_live(mutate, allowed=("inProgress",))

    def halt(self) -> dict:
        return self._update_live(lambda r: r.update(status="halted"), allowed=("inProgress",))

    def resume(self) -> dict:
        return self._update_live(lambda r: r.update(status="inProgress"), allowed=("halted",))

    def _update_live(self, mutate, allowed: tuple[str, ...]) -> dict:
        # Re-read inside the same edit right before writing, so a halt that
        # landed a moment ago is never overwritten by a rollout increase.
        edit = self.edits.insert(packageName=self.package, body={}).execute()
        track = self.edits.tracks().get(packageName=self.package, editId=edit["id"], track=self.track).execute()
        live = TrackState(track.get("releases", [])).live
        if not live or live["status"] not in allowed:
            self.edits.delete(packageName=self.package, editId=edit["id"]).execute()
            status = live["status"] if live else "none"
            raise RuntimeError(f"no {'/'.join(allowed)} release on {self.track} (current: {status})")
        release = {k: v for k, v in live.items() if k in ("name", "versionCodes", "status", "userFraction", "releaseNotes")}
        mutate(release)
        if self.dry_run:
            self.edits.delete(packageName=self.package, editId=edit["id"]).execute()
            print(f"[dry-run] would set release {release['name']} → {release}")
            return release
        self._commit_track(edit["id"], release)
        return release

    def _commit_track(self, edit_id: str, release: dict) -> None:
        self.edits.tracks().update(
            packageName=self.package, editId=edit_id, track=self.track,
            body={"track": self.track, "releases": [release]},
        ).execute()
        self.edits.commit(
            packageName=self.package, editId=edit_id,
            changesNotSentForReview=self.skip_review,
        ).execute()
