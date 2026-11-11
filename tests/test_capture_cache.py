"""Bounded capture admission is isolated by store and owner."""

from concurrent.futures import ThreadPoolExecutor

from measure_service.capture_cache import CaptureCache


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_other_owner_churn_does_not_evict_an_owner_capture():
    cache = CaptureCache(max_per_owner=2, max_entries=4, max_bytes=100)
    owner_a = (("store-a", 1, 10), "alice")
    owner_b = (("store-a", 1, 10), "bob")

    assert cache.put(owner_a, "a1", b"a1", 2)
    assert cache.put(owner_a, "a2", b"a2", 2)
    assert cache.put(owner_b, "b1", b"b1", 2)
    assert cache.put(owner_b, "b2", b"b2", 2)
    assert cache.get(owner_a, "a1") == b"a1"

    assert cache.put(owner_b, "b3", b"b3", 2)

    assert cache.get(owner_a, "a1") == b"a1"
    assert cache.get(owner_a, "a2") == b"a2"
    assert cache.get(owner_b, "b1") is None
    assert cache.get(owner_b, "b2") == b"b2"
    assert cache.get(owner_b, "b3") == b"b3"


def test_owner_lru_touch_controls_eviction():
    cache = CaptureCache(max_per_owner=2, max_entries=8, max_bytes=100)
    owner = (("store", 1, 20), "alice")

    assert cache.put(owner, "first", b"first", 5)
    assert cache.put(owner, "second", b"second", 6)
    assert cache.get(owner, "first") == b"first"
    assert cache.put(owner, "third", b"third", 5)

    assert cache.get(owner, "first") == b"first"
    assert cache.get(owner, "second") is None
    assert cache.get(owner, "third") == b"third"


def test_read_refreshes_idle_expiry_and_expired_entries_are_pruned():
    clock = Clock()
    cache = CaptureCache(
        max_per_owner=2,
        max_entries=8,
        max_bytes=100,
        idle_seconds=10,
        clock=clock,
    )
    owner = (("store", 1, 30), "alice")
    assert cache.put(owner, "capture", b"value", 5)

    clock.now = 8
    assert cache.get(owner, "capture") == b"value"
    clock.now = 17
    assert cache.get(owner, "capture") == b"value"
    clock.now = 27
    assert cache.get(owner, "capture") is None
    assert cache.capture_count == 0
    assert cache.serialized_bytes == 0


def test_rejected_admission_preserves_existing_captures_and_accounting():
    cache = CaptureCache(max_per_owner=2, max_entries=2, max_bytes=4)
    owner_a = (("store", 1, 40), "alice")
    owner_b = (("store", 1, 40), "bob")
    assert cache.put(owner_a, "kept", b"a", 1)
    assert cache.put(owner_b, "other", b"bbb", 3)

    assert not cache.put(owner_a, "new", b"xx", 2)

    assert cache.get(owner_a, "kept") == b"a"
    assert cache.get(owner_b, "other") == b"bbb"
    assert cache.get(owner_a, "new") is None
    assert cache.capture_count == 2
    assert cache.serialized_bytes == 4


def test_count_byte_owner_bounds_and_replacement_accounting():
    cache = CaptureCache(max_per_owner=2, max_entries=3, max_bytes=5)
    owner_a = (("store", 1, 50), "alice")
    owner_b = (("store", 1, 50), "bob")
    assert cache.put(owner_a, "a1", b"aa", 2)
    assert cache.put(owner_a, "a2", b"bb", 2)
    assert cache.put(owner_b, "b1", b"c", 1)
    assert cache.put(owner_a, "a3", b"dd", 2)
    assert cache.put(owner_b, "b2", b"e", 1)

    assert cache.capture_count == 3
    assert cache.serialized_bytes == 5
    assert cache.owner_count(owner_a) == 2
    assert cache.owner_count(owner_b) == 1

    assert cache.put(owner_a, "a3", b"x", 1)
    assert cache.capture_count == 3
    assert cache.serialized_bytes == 4
    assert cache.get(owner_a, "a3") == b"x"

    assert cache.put(owner_a, "zero", b"", 0)
    assert cache.capture_count == 3
    assert cache.serialized_bytes == 2
    cache.clear()
    assert cache.capture_count == 0
    assert cache.serialized_bytes == 0


def test_store_file_replacement_uses_a_distinct_owner_bucket():
    cache = CaptureCache(max_per_owner=4, max_entries=8, max_bytes=100)
    old_store = ("/tmp/measure.sqlite3", 10, 100)
    new_store = ("/tmp/measure.sqlite3", 10, 101)
    old_bucket = (old_store, "alice")
    new_bucket = (new_store, "alice")

    assert cache.put(old_bucket, "digest", b"old", 3)
    assert cache.get(new_bucket, "digest") is None
    assert cache.put(new_bucket, "digest", b"new", 3)
    assert cache.get(old_bucket, "digest") == b"old"
    assert cache.get(new_bucket, "digest") == b"new"


def test_concurrent_admission_keeps_count_bytes_and_owner_bounds():
    cache = CaptureCache(max_per_owner=3, max_entries=12, max_bytes=120)

    def churn(worker: int) -> None:
        owner = (("store", 1, 60), f"owner-{worker % 4}")
        for index in range(100):
            key = f"{worker}-{index}"
            value = key.encode()
            cache.put(owner, key, value, len(value))
            cache.get(owner, key)

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(churn, range(8)))

    assert cache.capture_count <= 12
    assert cache.serialized_bytes <= 120
    for owner_index in range(4):
        owner = (("store", 1, 60), f"owner-{owner_index}")
        assert cache.owner_count(owner) <= 3
