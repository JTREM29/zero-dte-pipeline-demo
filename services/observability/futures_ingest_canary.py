from __future__ import annotations

import time
from dataclasses import dataclass

from .futures_ingest_health import classify_futures_ingest


@dataclass(frozen=True)
class FuturesIngestSnapshot:
    hb_ts: int | None
    hb_age_s: int | None
    hb_msg: str | None
    scores_present: bool
    scores_ts: int | None
    scores_age_s: int | None
    regime: str | None
    note: str | None


def _norm_str(x: object | None) -> str | None:
    if x is None:
        return None
    s = str(x).strip()
    return s if s else None


def evaluate_futures_ingest_state(
    *,
    now_epoch: int | None,
    hb_ts: int | None,
    hb_msg: str | None,
    scores_updated_utc: int | None,
    scores_regime: str | None,
    status_note: str | None,
    hb_max_age_sec: int = 90,
    scores_max_age_sec: int = 120,
) -> tuple[str, FuturesIngestSnapshot]:
    """Return (state, snapshot) using the shared ingest classifier."""

    now_s = int(time.time()) if now_epoch is None else int(now_epoch)
    hb_max_age = max(1, int(hb_max_age_sec))
    scores_max_age = max(1, int(scores_max_age_sec))

    hb_ts_i = int(hb_ts) if hb_ts is not None else None
    hb_age = None
    if hb_ts_i is not None:
        hb_age = max(0, now_s - hb_ts_i)

    hb_msg_s = _norm_str(hb_msg)
    note_s = _norm_str(status_note)

    scores_ts = int(scores_updated_utc) if scores_updated_utc is not None else None
    scores_age = None
    scores_present = scores_ts is not None
    if scores_ts is not None:
        scores_age = max(0, now_s - scores_ts)

    regime_s = _norm_str(scores_regime)

    state, _reason = classify_futures_ingest(
        hb_age_s=hb_age,
        scores_age_s=scores_age,
        scores_present=bool(scores_present),
        note=note_s,
        hb_msg=hb_msg_s,
        hb_max_age=int(hb_max_age),
        scores_max_age=int(scores_max_age),
    )

    snap = FuturesIngestSnapshot(
        hb_ts=hb_ts_i,
        hb_age_s=hb_age,
        hb_msg=hb_msg_s,
        scores_present=bool(scores_present),
        scores_ts=scores_ts,
        scores_age_s=scores_age,
        regime=regime_s,
        note=note_s,
    )
    return state, snap


def _severity(state: str) -> int:
    s = (state or "").strip().upper()
    if s == "DOWN":
        return 2
    if s == "DEGRADED":
        return 1
    return 0


class FuturesIngestCanary:
    """State-change-only canary for futures ingest health.

    Noise controls:
    - Never posts an initial OK on startup.
    - Always posts when severity increases (e.g., OK -> DEGRADED, DEGRADED -> DOWN).
    - Optionally rate-limits recovery messages via min_post_sec.
    """

    def __init__(self, *, min_post_sec: int = 300):
        self.min_post_sec = int(max(0, min(int(min_post_sec), 24 * 3600)))
        self._last_state: str | None = None
        self._last_post_s: int | None = None

    def maybe_message(
        self,
        *,
        now_epoch: int | None,
        hb_ts: int | None,
        hb_msg: str | None,
        scores_updated_utc: int | None,
        scores_regime: str | None,
        status_note: str | None,
        hb_max_age_sec: int = 90,
        scores_max_age_sec: int = 120,
    ) -> str | None:
        now_s = int(time.time()) if now_epoch is None else int(now_epoch)
        state, snap = evaluate_futures_ingest_state(
            now_epoch=now_s,
            hb_ts=hb_ts,
            hb_msg=hb_msg,
            scores_updated_utc=scores_updated_utc,
            scores_regime=scores_regime,
            status_note=status_note,
            hb_max_age_sec=int(hb_max_age_sec),
            scores_max_age_sec=int(scores_max_age_sec),
        )
        _state2, reason = classify_futures_ingest(
            hb_age_s=snap.hb_age_s,
            scores_age_s=snap.scores_age_s,
            scores_present=bool(snap.scores_present),
            note=snap.note,
            hb_msg=snap.hb_msg,
            hb_max_age=int(hb_max_age_sec),
            scores_max_age=int(scores_max_age_sec),
        )

        state_u = state.upper()
        if self._last_state is None:
            self._last_state = state_u
            if state_u == "OK":
                return None
            self._last_post_s = now_s
            return self._format_message(state_u, snap, reason=reason)

        if state_u == self._last_state:
            return None

        prev = self._last_state
        prev_sev = _severity(prev)
        new_sev = _severity(state_u)

        should_post = False
        if new_sev > prev_sev:
            should_post = True
        else:
            # recovery: optionally rate-limit
            if self.min_post_sec <= 0:
                should_post = True
            else:
                last_post = self._last_post_s or 0
                if (now_s - int(last_post)) >= int(self.min_post_sec):
                    should_post = True

        self._last_state = state_u
        if should_post:
            self._last_post_s = now_s
            return self._format_message(state_u, snap, prev_state=prev, reason=reason)
        return None

    @staticmethod
    def _format_message(
        state: str,
        snap: FuturesIngestSnapshot,
        prev_state: str | None = None,
        reason: str | None = None,
    ) -> str:
        st = (state or "OK").upper()
        if st == "DOWN":
            prefix = "🚨 Futures ingest DOWN"
        elif st == "DEGRADED":
            prefix = "⚠️ Futures ingest DEGRADED"
        else:
            prefix = "✅ Futures ingest OK"

        bits: list[str] = []
        if prev_state:
            bits.append(f"{prev_state}→{st}")
        if snap.hb_age_s is None:
            bits.append("hb missing")
        else:
            bits.append(f"hb {snap.hb_age_s}s")
        if snap.scores_present:
            if snap.scores_age_s is not None:
                bits.append(f"scores {snap.scores_age_s}s")
            else:
                bits.append("scores present")
        else:
            bits.append("scores missing")
        if snap.regime:
            bits.append(f"regime={snap.regime}")
        r = (reason or "").strip()
        if r and r not in {"hb missing", "scores missing"}:
            bits.append(r)

        return prefix + " — " + "; ".join(bits)
