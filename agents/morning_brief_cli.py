import argparse
import asyncio
import json
from typing import Iterable, Optional

from agents.morning_brief_agent import analyze_brief_with_openai, build_brief


def _resolve_symbols(symbol_args: Iterable[str]) -> Optional[list[str]]:
    symbols = [sym for sym in symbol_args if sym]
    return symbols or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and optionally analyze morning briefs")
    sub = parser.add_subparsers(dest="command")

    mb = sub.add_parser("morning-brief", help="Build brief and run OpenAI analysis unless disabled")
    mb.add_argument("--symbol", action="append", default=[], help="Ticker symbol (repeatable)")
    mb.add_argument("--no-openai", action="store_true", help="Skip OpenAI analysis")

    mb_core = sub.add_parser("morning-brief-core", help="Build brief only")
    mb_core.add_argument("--symbol", action="append", default=[], help="Ticker symbol (repeatable)")

    args = parser.parse_args()

    if args.command in ("morning-brief", "morning-brief-core"):
        symbols = _resolve_symbols(args.symbol)
        brief = asyncio.run(build_brief(symbols=symbols))

        if args.command == "morning-brief-core" or getattr(args, "no_openai", False):
            print(json.dumps(brief, indent=2))
        else:
            analyzed = asyncio.run(analyze_brief_with_openai(brief))
            print(json.dumps(analyzed, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
