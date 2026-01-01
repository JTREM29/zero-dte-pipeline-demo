import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
	sys.path.insert(0, str(PROJECT_ROOT))

from delivery import discord_bot as delivery

os.environ.setdefault("SYMBOLS", "SPY,QQQ,IWM")

print("daily", len(delivery.build_daily_prep_render(["SPY", "QQQ"]).text))
print("focus", len(delivery.build_focus_list_render(["SPY", "QQQ"]).text))
print("intraday", len(delivery.build_intraday_update_render(["SPY", "QQQ"]).text))
payload = delivery._build_agent_payload(["SPY"], generated_at=delivery._now_et(), post_type="status", extra_meta={})
print("agent payload", isinstance(payload, dict))
