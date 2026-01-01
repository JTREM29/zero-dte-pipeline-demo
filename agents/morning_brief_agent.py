import datetime
import json
from dataclasses import asdict
from typing import Dict, Iterable, Optional

from analysis.indicators import compute_indicators_for_symbol
from data.fetchers import get_global_context, get_symbol_context
from data.ibkr_client import IBKRClient
from data.massive_client import MassiveClient
from delivery.state_builder import build_tnt_state
from zero_dte_pipeline.config import config
from zero_dte_pipeline.openai_client import OpenAIClient, OpenAIError

DEFAULT_SYMBOLS = ["QQQ", "IWM", "SPY", "SPX"]


async def build_brief(symbols: Optional[Iterable[str]] = None):
    if symbols is None:
        targets = list(DEFAULT_SYMBOLS)
    else:
        targets = [s for s in symbols]

    async with MassiveClient(
        api_key=config.massive_api_key,
        base_url=config.massive_base_url,
    ) as massive:
        ibkr = IBKRClient(
            host=config.ibkr_host,
            port=config.ibkr_port,
            client_id=config.ibkr_client_id,
        )

        ibkr_available = True
        ibkr_error: Optional[str] = None
        try:
            await ibkr.connect()
        except Exception as exc:  # pragma: no cover - external dependency failure
            ibkr_available = False
            ibkr_error = str(exc)

        try:
            symbol_tasks = [get_symbol_context(massive, sym) for sym in targets]
            global_task = get_global_context(massive, ibkr if ibkr_available else None)

            symbol_contexts = await gather_symbol_contexts(symbol_tasks)
            global_ctx = await global_task

            vix_value = extract_vix_value(global_ctx)

            enriched = []
            for ctx in symbol_contexts:
                entry = asdict(ctx)
                daily_bars = entry.get("daily_bars", []) or []
                entry["indicators"] = compute_indicators_for_symbol(
                    daily_bars,
                    volatility_proxy=vix_value,
                )
                entry["last_price"] = derive_last_price(entry, daily_bars)
                enriched.append(entry)

            global_dict = asdict(global_ctx)
            if ibkr_error:
                global_dict["ibkr_error"] = ibkr_error

            return {"symbols": enriched, "global": global_dict}
        finally:
            await ibkr.disconnect()


def derive_last_price(context: Dict, daily_bars):
    quote = context.get("quote") or {}
    last_price: Optional[float] = None

    if isinstance(quote, dict):
        raw_last = (
            quote.get("last")
            or quote.get("price")
            or quote.get("close")
        )
        if isinstance(raw_last, dict):
            raw_last = (
                raw_last.get("price")
                or raw_last.get("p")
                or raw_last.get("last")
            )
        try:
            if raw_last is not None:
                last_price = float(raw_last)
        except (TypeError, ValueError):  # pragma: no cover - defensive casting
            last_price = None

    if last_price is None and daily_bars:
        try:
            last_price = float(daily_bars[-1].get("c"))
        except (TypeError, ValueError, AttributeError):  # pragma: no cover - defensive casting
            last_price = None

    return last_price


def extract_vix_value(global_ctx) -> Optional[float]:
    vix_quote = getattr(global_ctx, "vix_quote", {}) or {}
    if not isinstance(vix_quote, dict):
        return None

    raw_vix = vix_quote.get("last") or vix_quote.get("price")
    if isinstance(raw_vix, dict):
        raw_vix = raw_vix.get("price") or raw_vix.get("p")
    try:
        if raw_vix is not None:
            return float(raw_vix)
    except (TypeError, ValueError):  # pragma: no cover - defensive casting
        return None
    return None


async def gather_symbol_contexts(tasks):
    from asyncio import gather

    return await gather(*tasks)


SYSTEM_PROMPT = """
You are a professional technical market analyst.
You analyze structured JSON market data (daily bars, intraday bars, quotes, VIX, ES) and produce a clear, human-readable summary for each symbol.

Your output must follow this format for each symbol:

1. "<SYMBOL> Outlook (<Date Range>)" as a markdown heading (##)
2. ASCII Price Zone Sketch (visual support/resistance). Use approximate levels based on last price, supports, and resistances.
3. Key Indicators:
   - 5-day SMA (value + slope: up/down/flat)
   - 10-day SMA
   - RSI(14)
   - MACD (positive/negative, histogram rising/falling)
   - ATR(10)
   - Volatility context (VIX or proxy, from global section)
4. Support & Resistance Levels:
   - At least 2 support levels
   - At least 2 resistance levels
5. Bullish Scenario
6. Bearish Scenario
7. Summary Table:

   | Indicator | Value | Bullish Signal? |
   |----------|-------|-----------------|

8. Agent Takeaway:
   One or two sentences with a clear conclusion about bias (bullish / bearish / choppy) and whether 0DTE directional trades are favored or should be avoided.

Use simple language and keep the output visually clean. Do not include raw JSON in the answer.
""".strip()


def _today_range_str() -> str:
    today = datetime.date.today()
    start = today - datetime.timedelta(days=today.weekday())
    end = start + datetime.timedelta(days=4)
    return f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}"


def _build_user_prompt(brief: dict) -> str:
    date_range = _today_range_str()
    brief_json = json.dumps(brief)
    return (
        f"Date context / report range: {date_range}\n\n"
        "Below is structured market context in JSON format. The 'symbols' list "
        "contains one object per symbol with attributes such as symbol, "
        "last_price, daily_bars, intraday_bars, indicators, and status. The "
        "'global' section contains VIX and ES context.\n\n"
        "JSON input:\n"
        f"{brief_json}\n\n"
        "For EACH symbol in the same order they appear in the JSON, generate a "
        "full technical outlook using the exact format described in the system "
        "prompt. Focus on trend direction, support/resistance zones, indicator "
        "strength/weakness, and whether the environment favors bullish, bearish, "
        "or choppy conditions.\n"
    )


async def analyze_brief_with_openai(brief: dict) -> dict:
    user_prompt = _build_user_prompt(brief)
    primary_symbol = "SPY"
    try:
        symbols = brief.get("symbols") if isinstance(brief, dict) else None
        if isinstance(symbols, list) and symbols:
            first = symbols[0]
            if isinstance(first, dict) and first.get("symbol"):
                primary_symbol = str(first.get("symbol") or "SPY").upper()
    except Exception:  # noqa: BLE001
        primary_symbol = "SPY"

    tnt_state = build_tnt_state([primary_symbol], mode="ON_DEMAND")
    client = OpenAIClient()
    try:
        text = client.complete(
            system=SYSTEM_PROMPT,
            prompt=user_prompt,
            tnt_state=tnt_state,
            model="gpt-4.1-mini",
            temperature=0.2,
        )
    except OpenAIError as exc:
        text = f"OpenAI analysis unavailable: {exc}"

    return {"brief": brief, "agent_analysis": text}
