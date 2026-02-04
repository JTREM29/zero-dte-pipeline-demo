import json
import time
import types


def test_ctx_status_module_is_dependency_light() -> None:
    import cli.ctx_status as mod

    path = mod.__file__
    assert path
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    forbidden = [
        "import delivery.discord_bot",
        "from delivery import discord_bot",
        "import controller.worker_pool",
        "enqueue_job",
        "smb",
    ]
    for token in forbidden:
        assert token not in src


def test_ctx_status_reports_redis_config_and_key_ages(monkeypatch) -> None:
    now = int(time.time())

    class _DummyRedis:
        def __init__(self, **kwargs):
            # Ensure we keep short timeouts.
            assert "socket_timeout" in kwargs
            assert "socket_connect_timeout" in kwargs

        def ping(self):
            return True

        def info(self, section=None):
            assert section in (None, "server")
            return {"run_id": "abcd1234efgh5678"}

        def dbsize(self):
            return 4242

        def get(self, key):
            if key == "ctx:market":
                return json.dumps({"ts_utc": now - 3})
            if key == "ctx:sym:SPY":
                return json.dumps({"ts_utc": now - 7})
            if key == "ctx:last_write_ts":
                return str(now - 5)
            if key == "ctx:last_write_count":
                return "5"
            if key == "ctx:last_write_seq":
                return "123"
            if key == "ctx:last_writer_host":
                return "TESTHOST"
            if key == "ctx:last_writer_pid":
                return "999"
            return None

        def scan_iter(self, match=None, count=None):
            return iter([])

    dummy = types.SimpleNamespace(Redis=_DummyRedis)
    monkeypatch.setitem(__import__("sys").modules, "redis", dummy)

    monkeypatch.setenv("TNT_REDIS_HOST", "10.0.0.9")
    monkeypatch.setenv("TNT_REDIS_PORT", "6380")
    monkeypatch.setenv("TNT_REDIS_DB", "2")

    from cli.ctx_status import build_ctx_status_message

    msg = build_ctx_status_message(symbol="SPY", count_keys=False, timeout_s=0.25)
    assert "Redis (bot runtime): host=10.0.0.9 port=6380 db=2" in msg
    assert "run_id=abcd1234" in msg
    assert "dbsize=4242" in msg
    assert "Ping: ok" in msg
    assert "ctx:market: exists=True" in msg
    assert "ctx:sym:SPY: exists=True" in msg
    assert "writer_meta:" in msg


def test_ctx_status_key_count_is_capped(monkeypatch) -> None:
    now = int(time.time())

    class _DummyRedis:
        def __init__(self, **kwargs):
            pass

        def ping(self):
            return True

        def info(self, section=None):
            return {"run_id": "zzzz9999yyyy8888"}

        def dbsize(self):
            return 10

        def get(self, key):
            if key == "ctx:market":
                return json.dumps({"ts_utc": now})
            if key == "ctx:sym:SPY":
                return json.dumps({"ts_utc": now})
            return None

        def scan_iter(self, match=None, count=None):
            assert match == "ctx:*"
            for i in range(6000):
                yield f"ctx:key:{i}"

    dummy = types.SimpleNamespace(Redis=_DummyRedis)
    monkeypatch.setitem(__import__("sys").modules, "redis", dummy)

    from cli.ctx_status import build_ctx_status_message

    msg = build_ctx_status_message(symbol="SPY", count_keys=True, timeout_s=0.01)
    assert "ctx:* key_count:" in msg
    assert "capped" in msg
