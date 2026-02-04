from __future__ import annotations

from services.triple_expiry.locks import acquire_lock, release_lock


class FakeRedis:
    def __init__(self):
        self.kv = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)

    def delete(self, key):
        self.kv.pop(key, None)
        return 1


def test_lock_is_single_flight_and_token_checked() -> None:
    r = FakeRedis()
    h1 = acquire_lock(r, "lock:test", ttl_sec=10)
    assert h1 is not None

    h2 = acquire_lock(r, "lock:test", ttl_sec=10)
    assert h2 is None

    assert release_lock(r, h1) is True

    # Now it can be acquired again.
    h3 = acquire_lock(r, "lock:test", ttl_sec=10)
    assert h3 is not None
