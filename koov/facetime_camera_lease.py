"""Host V4L2 camera-device leases for containerized FaceTime workers.

The API process may itself run in Docker and therefore does not need to see the
host device nodes.  The host Docker daemon resolves ``docker run --device``.
Lease metadata prevents two sibling workers from receiving the same camera.
"""

from __future__ import annotations

from koov.filelock import lock_exclusive, unlock
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


_DEVICE_RE = re.compile(r"^/dev/video[0-9]+$")


def configured_video_devices(value: str | None = None) -> tuple[str, ...]:
    """Return a validated, de-duplicated virtual-camera pool."""
    raw = os.environ.get("KOOV_FT_VIDEO_DEVICES", "") if value is None else value
    out: list[str] = []
    seen: set[str] = set()
    for item in str(raw or "").split(","):
        device = item.strip()
        if not device:
            continue
        if not _DEVICE_RE.fullmatch(device):
            raise ValueError(f"Invalid FaceTime video device: {device!r}")
        if device not in seen:
            seen.add(device)
            out.append(device)
    return tuple(out)


@dataclass(frozen=True)
class CameraLease:
    device: str
    meeting_id: str
    container_name: str
    acquired_at: float

    @classmethod
    def from_dict(cls, value: object) -> "CameraLease | None":
        if not isinstance(value, dict):
            return None
        try:
            device = str(value.get("device") or "").strip()
            meeting_id = str(value.get("meeting_id") or "").strip()
            container_name = str(value.get("container_name") or "").strip()
            acquired_at = float(value.get("acquired_at") or 0.0)
        except (TypeError, ValueError):
            return None
        if not _DEVICE_RE.fullmatch(device) or not meeting_id or not container_name:
            return None
        return cls(device, meeting_id, container_name, acquired_at)


class CameraLeaseManager:
    """Small file-locked lease store shared by API server processes."""

    def __init__(
        self,
        notes_dir: str | os.PathLike[str],
        devices: Iterable[str] | None = None,
    ) -> None:
        self.notes_dir = Path(notes_dir).resolve()
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.devices = tuple(devices) if devices is not None else configured_video_devices()
        for device in self.devices:
            if not _DEVICE_RE.fullmatch(device):
                raise ValueError(f"Invalid FaceTime video device: {device!r}")
        if len(set(self.devices)) != len(self.devices):
            raise ValueError("FaceTime video device pool contains duplicates")
        self.state_path = self.notes_dir / ".facetime-camera-leases.json"
        self.lock_path = self.notes_dir / ".facetime-camera-leases.lock"

    def _read_unlocked(self) -> list[CameraLease]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        rows = value.get("leases", []) if isinstance(value, dict) else []
        result: list[CameraLease] = []
        for row in rows if isinstance(rows, list) else []:
            lease = CameraLease.from_dict(row)
            if lease is not None and lease.device in self.devices:
                result.append(lease)
        return result

    def _write_unlocked(self, leases: list[CameraLease]) -> None:
        payload = {
            "schema_version": 1,
            "updated_at": time.time(),
            "leases": [asdict(item) for item in leases],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.state_path.name + ".",
            suffix=".tmp",
            dir=str(self.notes_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.state_path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def _locked(self):
        manager = self

        class _LeaseLock:
            def __enter__(self):
                self.handle = open(manager.lock_path, "a+", encoding="utf-8")
                lock_exclusive(self.handle)
                return self

            def __exit__(self, exc_type, exc, tb):
                unlock(self.handle)
                self.handle.close()

        return _LeaseLock()

    def acquire(self, meeting_id: str, container_name: str) -> CameraLease | None:
        meeting_id = str(meeting_id or "").strip()
        container_name = str(container_name or "").strip()
        if not meeting_id or not container_name:
            raise ValueError("meeting_id and container_name are required")
        with self._locked():
            leases = self._read_unlocked()
            for lease in leases:
                if lease.meeting_id == meeting_id:
                    return lease
            used = {lease.device for lease in leases}
            device = next((item for item in self.devices if item not in used), None)
            if device is None:
                return None
            lease = CameraLease(device, meeting_id, container_name, time.time())
            leases.append(lease)
            self._write_unlocked(leases)
            return lease

    def release(self, meeting_id: str) -> None:
        meeting_id = str(meeting_id or "").strip()
        if not meeting_id:
            return
        with self._locked():
            leases = self._read_unlocked()
            kept = [lease for lease in leases if lease.meeting_id != meeting_id]
            if kept != leases:
                self._write_unlocked(kept)

    def reconcile(
        self,
        running_claims: Iterable[CameraLease],
        *,
        grace_s: float = 180.0,
        now: float | None = None,
    ) -> list[CameraLease]:
        """Replace stale metadata with validated claims from running containers.

        A lease acquired moments ago may belong to a join whose ``docker run``
        has not started yet, so it is not visible in ``docker ps``. Such
        unconfirmed leases are kept for ``grace_s`` seconds; otherwise a
        concurrent join could reconcile them away and receive the same device.
        """
        current_time = time.time() if now is None else float(now)
        by_device: dict[str, CameraLease] = {}
        by_meeting: set[str] = set()
        for claim in running_claims:
            if claim.device not in self.devices or claim.meeting_id in by_meeting:
                continue
            if claim.device in by_device:
                continue
            by_device[claim.device] = claim
            by_meeting.add(claim.meeting_id)
        with self._locked():
            for lease in self._read_unlocked():
                if lease.device in by_device or lease.meeting_id in by_meeting:
                    continue
                if current_time - lease.acquired_at < max(0.0, grace_s):
                    by_device[lease.device] = lease
                    by_meeting.add(lease.meeting_id)
            leases = [by_device[d] for d in self.devices if d in by_device]
            self._write_unlocked(leases)
        return leases

    def snapshot(self) -> list[CameraLease]:
        with self._locked():
            return list(self._read_unlocked())

