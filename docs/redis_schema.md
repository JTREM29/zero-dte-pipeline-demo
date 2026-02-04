# Redis schema (TNT)

## `oi:walls:{SYMBOL}`
- Type: Redis hash
- TTL: `OI_WALLS_TTL_SEC` (default `1800` seconds)
- Writer: options OI/IV computation path (Discord `/oi` and options microstructure summary)
- Reader: divergence enrichment (`technicals.divergence.divergence_trade_plan` via `technicals.oi_walls.get_oi_walls`)

Fields:
- `call_wall`: float strike of max call OI bucket
- `put_wall`: float strike of max put OI bucket
- `ts_et`: ISO timestamp string (America/New_York wall “as-of”)
- `px`: underlying price used for the chain snapshot (optional)
- `call_oi`, `put_oi`: max OI at the wall strikes (optional)
- `expiry`: expiration label used for the chain snapshot (optional)

Example:
- `HGETALL oi:walls:SPY` → `{call_wall: "485", put_wall: "480", ts_et: "2026-01-21T16:03:12-05:00", px: "484.92", call_oi: "120034", put_oi: "110882", expiry: "2026-01-21"}`
