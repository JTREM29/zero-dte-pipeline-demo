#!/usr/bin/env python
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from textwrap import dedent

import requests


def _run_json(cmd: list[str]) -> dict:
    out = subprocess.check_output(cmd, text=True)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        print("!! Failed to decode JSON output from command:")
        print("Command:", " ".join(cmd))
        print("Raw output:")
        print(out)
        sys.exit(1)


def _run_morning_report(symbol: str) -> dict:
    cmd = [
        sys.executable,
        "-m",
        "cli.main",
        "--json-output",
        "morning-report",
        "--symbol",
        symbol,
    ]
    return _run_json(cmd)


def _run_headline_summary() -> dict:
    cmd = [
        sys.executable,
        "-m",
        "cli.main",
        "--json-output",
        "summarize-headlines",
    ]
    return _run_json(cmd)


def _get(data: dict, key: str, default=None):
    return data.get(key, default)


def _format_brief(report: dict, headlines: dict, symbol: str) -> str:
    ts = report.get("timestamp_utc") or dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    price = report.get("price")
    change_abs = report.get("change_abs")
    change_pct = report.get("change_pct")

    headline_list = headlines.get("headlines", [])
    bullet_summary = headlines.get("bullets", "")
    news_bias = headlines.get("bias", "neutral")
    news_score = headlines.get("sentiment_score", 0.0)

    next_day_bias = report.get("next_day_bias")
    next_day_conf = report.get("next_day_conf")
    eod_bias = report.get("eod_bias")
    eod_conf = report.get("eod_conf")

    trade_regime = report.get("trade_regime", "UNKNOWN")
    suggested_size = report.get("suggested_size", 0.0)

    lines = []

    lines.append(f"🔔 **Morning Market Brief — {ts} — {symbol}**\n")

    lines.append("## 📊 Index Snapshot")
    if price is not None:
        lines.append(f"**Price:** {price:.2f}")
    if change_abs is not None and change_pct is not None:
        lines.append(f"**Change:** {change_abs:+.2f} ({change_pct:+.2f}%)")
    lines.append("")

    lines.append("## 📰 Top Headlines (AI-summarized)")
    lines.append(f"**Bias:** `{news_bias}` | **Score:** `{news_score:+.2f}`")
    lines.append("")
    if bullet_summary:
        lines.append(bullet_summary)
        lines.append("")
    if headline_list:
        lines.append("### Raw Headlines:")
        for h in headline_list[:8]:
            lines.append(f"- {h}")
        lines.append("")

    lines.append("## 📈 Outlook")
    if next_day_conf is not None:
        lines.append(f"**Next-Day:** {next_day_bias} (conf {next_day_conf:.2f})")
    if eod_conf is not None:
        lines.append(f"**EOD:** {eod_bias} (conf {eod_conf:.2f})")
    lines.append("")

    lines.append("## 🎛 Current Stance")
    lines.append(f"**Regime:** {trade_regime}")
    lines.append(f"**Suggested Size:** {suggested_size:.2f}")
    if suggested_size <= 0 or trade_regime in ("UNKNOWN", "HOSTILE", "NO_TRADE"):
        lines.append("➡ **Action:** No trade — informational only.")
    else:
        lines.append("➡ **Action:** Environment permissive for *considering* risk (not advice).")
    lines.append("")

    lines.append("_This brief is informational — not trading advice._")

    return "\n".join(lines)


def _post_to_discord(message: str):
    url = os.getenv("DISCORD_MORNING_BRIEF_WEBHOOK")
    if not url:
        print("⚠ No DISCORD_MORNING_BRIEF_WEBHOOK set — skipping Discord post.")
        return

    payload = {
        "content": None,
        "embeds": [
            {
                "title": "Morning Market Brief",
                "description": message[:4000],
                "type": "rich",
            }
        ],
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code >= 300:
            print("⚠ Discord error:", r.status_code, r.text)
        else:
            print("✅ Morning brief posted to Discord.")
    except Exception as e:
        print("⚠ Failed posting to Discord:", e)


def main(argv=None):
    argv = argv or sys.argv[1:]

    symbol = "SPX"
    post = "--post" in argv or "-p" in argv

    for arg in argv:
        if not arg.startswith("-"):
            symbol = arg.upper()

    print("Running morning report…")
    report = _run_morning_report(symbol)

    print("Running OpenAI headline summary…")
    headlines = _run_headline_summary()

    brief = _format_brief(report, headlines, symbol)

    print("\n" + brief)

    if post:
        _post_to_discord(brief)


if __name__ == "__main__":
    main()
