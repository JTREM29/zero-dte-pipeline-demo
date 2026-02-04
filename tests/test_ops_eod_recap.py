from __future__ import annotations

import os
from datetime import datetime, timezone

from controller.ops_metrics import OpsMetrics


def test_eod_recap_env_gated_off_by_default(tmp_path) -> None:
    os.environ.pop("TNT_EOD_RECAP_ENABLED", None)
    m = OpsMetrics()
    wrote = m.maybe_write_eod_recap(now_epoch=0, root_dir=tmp_path, tail_lines=0)
    assert wrote is False


def test_eod_recap_writes_files_once_when_enabled(tmp_path) -> None:
    os.environ["TNT_EOD_RECAP_ENABLED"] = "1"
    os.environ["TNT_EOD_RECAP_TZ"] = "UTC"

    m = OpsMetrics()

    # Simulate a time after the threshold (16:15 UTC) for a stable date.
    dt = datetime(2026, 1, 25, 16, 20, tzinfo=timezone.utc)
    now_epoch = int(dt.timestamp())

    # Ensure we have some counters.
    m.update_tick("OK", inflight=3, dt_s=120)

    wrote1 = m.maybe_write_eod_recap(now_epoch=now_epoch, at_hour=16, at_minute=15, root_dir=tmp_path, tail_lines=0)
    assert wrote1 is True

    out_path = tmp_path / "logs" / "ops_metrics" / "eod_recap_2026-01-25.txt"
    latest_path = tmp_path / "logs" / "eod_recap_latest.txt"

    assert out_path.exists()
    assert latest_path.exists()

    # Second call same day should not rewrite.
    wrote2 = m.maybe_write_eod_recap(now_epoch=now_epoch, at_hour=16, at_minute=15, root_dir=tmp_path, tail_lines=0)
    assert wrote2 is False
