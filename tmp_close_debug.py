import os, json, datetime as dt
from zoneinfo import ZoneInfo

print("=== TIME ===")
now_utc = dt.datetime.now(dt.timezone.utc)
now_et  = now_utc.astimezone(ZoneInfo("America/New_York"))
print("now_utc:", now_utc.isoformat())
print("now_et :", now_et.isoformat())

print("\n=== TRY: import delivery.discord_bot ===")
try:
    from delivery import discord_bot as clb
    print("OK imported delivery.discord_bot from:", clb.__file__)
except Exception as e:
    print("IMPORT ERROR:", type(e).__name__, e)
    raise

print("\n=== TRY: get_latest_daily_pivots() ===")
try:
    piv = clb.get_latest_daily_pivots("SPY")
    print("piv:", piv)
except Exception as e:
    print("ERROR get_latest_daily_pivots:", type(e).__name__, e)

print("\n=== TRY: get_last_rth_session_ohlc('SPY') ===")
try:
    ohlc = clb.get_last_rth_session_ohlc("SPY")
    print("ohlc:", ohlc)
except Exception as e:
    print("ERROR get_last_rth_session_ohlc:", type(e).__name__, e)

print("\n=== QUICK: find any SPY minute files under repo ===")
hits = 0
for root, dirs, files in os.walk("."):
    for fn in files:
        s = fn.lower()
        if "spy" in s and ("1m" in s or "minute" in s or "bars" in s):
            hits += 1
            if hits <= 20:
                print("hit:", os.path.join(root, fn))
print("hits:", hits)
