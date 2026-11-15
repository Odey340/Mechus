import threading

import pytest

from mechus.facetime_camera_lease import CameraLease, CameraLeaseManager, configured_video_devices


def test_configured_video_devices_validates_and_deduplicates():
    assert configured_video_devices(" /dev/video20, /dev/video21,/dev/video20 ") == (
        "/dev/video20",
        "/dev/video21",
    )
    assert configured_video_devices("") == ()
    with pytest.raises(ValueError):
        configured_video_devices("/dev/video20,/dev/sda")


def test_camera_lease_allocate_release_and_reconcile(tmp_path):
    manager = CameraLeaseManager(tmp_path, ["/dev/video20", "/dev/video21"])
    first = manager.acquire("m1", "c1")
    second = manager.acquire("m2", "c2")
    assert first and first.device == "/dev/video20"
    assert second and second.device == "/dev/video21"
    assert manager.acquire("m3", "c3") is None
    assert manager.acquire("m1", "c1") == first

    manager.release("m1")
    third = manager.acquire("m3", "c3")
    assert third and third.device == "/dev/video20"

    # With no grace period only running-container claims survive.
    claims = [CameraLease("/dev/video21", "m2", "c2", 1.0)]
    assert manager.reconcile(claims, grace_s=0) == claims
    assert manager.snapshot() == claims


def test_reconcile_keeps_fresh_unconfirmed_lease(tmp_path):
    """Join A leased a device but its container is not running yet; join B
    reconciles. A's lease must survive so B cannot receive the same device."""
    manager = CameraLeaseManager(tmp_path, ["/dev/video20", "/dev/video21"])
    a = manager.acquire("meeting-a", "mechus-facetime-a")
    assert a is not None
    manager.reconcile([], grace_s=180)  # docker ps shows nothing yet
    b = manager.acquire("meeting-b", "mechus-facetime-b")
    assert b is not None and b.device != a.device


def test_reconcile_drops_stale_unconfirmed_lease(tmp_path):
    manager = CameraLeaseManager(tmp_path, ["/dev/video20"])
    lease = manager.acquire("old", "mechus-facetime-old")
    assert lease is not None
    kept = manager.reconcile([], grace_s=180, now=lease.acquired_at + 600)
    assert kept == []
    assert manager.acquire("new", "mechus-facetime-new") is not None


def test_reconcile_running_claim_wins_over_conflicting_lease(tmp_path):
    manager = CameraLeaseManager(tmp_path, ["/dev/video20", "/dev/video21"])
    manager.acquire("m1", "c1")  # recorded on /dev/video20
    running = [CameraLease("/dev/video20", "m9", "c9", 5.0)]
    leases = manager.reconcile(running, grace_s=180)
    assert [(l.device, l.meeting_id) for l in leases] == [("/dev/video20", "m9")]


def test_concurrent_acquire_gives_unique_devices(tmp_path):
    devices = [f"/dev/video{20 + i}" for i in range(8)]
    results: list[CameraLease | None] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        lease = CameraLeaseManager(tmp_path, devices).acquire(f"m{i}", f"c{i}")
        with lock:
            results.append(lease)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    granted = [r for r in results if r is not None]
    assert len(granted) == 8
    assert len({r.device for r in granted}) == 8
    assert results.count(None) == 4


def test_camera_lease_rejects_invalid_device(tmp_path):
    with pytest.raises(ValueError, match="Invalid FaceTime video device"):
        CameraLeaseManager(tmp_path, ["/tmp/not-a-camera"])
    with pytest.raises(ValueError, match="duplicates"):
        CameraLeaseManager(tmp_path, ["/dev/video20", "/dev/video20"])
