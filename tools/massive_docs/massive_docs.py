#!/usr/bin/env python3
"""Massive llms.txt docs cacher for TNT.

Caches Massive docs (llms.txt indexes + selected linked .md endpoint pages) locally,
and generates a single bundle file per section you can point agents at.

Usage:
  python tools/massive_docs/massive_docs.py refresh --sections rest websocket --max-endpoints 120
  python tools/massive_docs/massive_docs.py refresh --sections rest/options rest/stocks
  python tools/massive_docs/massive_docs.py search "expected move"
  python tools/massive_docs/massive_docs.py list

Environment overrides:
  MASSIVE_DOCS_BASE_URL  e.g. https://massive.com (default)

Notes:
  - This tool only fetches markdown/text endpoints exposed in llms.txt.
  - It writes under tools/massive_docs/cache/.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# Prefer env override so this works in staging/proxy setups.
_DOCS_BASE_HOST = (os.getenv("MASSIVE_DOCS_BASE_URL") or "https://massive.com").rstrip("/")
BASE = f"{_DOCS_BASE_HOST}/docs"

CACHE_ROOT = Path("tools/massive_docs/cache")
UA = "TNT-MassiveDocsCacher/1.0"

DEFAULT_SECTIONS = [
    "rest",  # master REST index
    "rest/stocks",  # common TNT needs
    "rest/options",  # chains / OI / expected move
    "websocket",  # WS docs
]

# Keep it conservative; raise if you want fuller caching
DEFAULT_MAX_ENDPOINTS = 120


@dataclass
class FetchResult:
    url: str
    status: str
    bytes_written: int
    path: Path


def _http_get_text(url: str, timeout_s: int = 20) -> str:
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/plain, text/markdown, */*"})
    with urlopen(req, timeout=timeout_s) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _safe_path_from_url(url: str) -> Path:
    """Turn a docs URL into a stable local path under CACHE_ROOT."""

    u = urlparse(url)
    path = u.path
    if "/docs/" in path:
        path = path.split("/docs/", 1)[1]
    path = path.lstrip("/")
    return CACHE_ROOT / path


def _write_text(path: Path, text: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def _extract_markdown_links(md: str) -> List[str]:
    """Extract all markdown links: [text](url)."""

    return re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", md)


def _is_massive_docs_url(url: str) -> bool:
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return False
        # Accept the configured host as well as massive.com.
        allowed_hosts = {"massive.com", urlparse(_DOCS_BASE_HOST).netloc}
        if u.netloc not in allowed_hosts:
            return False
        return u.path.startswith("/docs/")
    except Exception:
        return False


def _is_endpoint_md(url: str) -> bool:
    return url.endswith(".md")


def _fetch_and_cache(url: str) -> FetchResult:
    text = _http_get_text(url)
    out_path = _safe_path_from_url(url)
    n = _write_text(out_path, text)
    return FetchResult(url=url, status="ok", bytes_written=n, path=out_path)


def refresh_section(section: str, max_endpoints: int) -> List[FetchResult]:
    """Refresh section index + linked endpoint markdown pages + bundle."""

    results: List[FetchResult] = []

    section = section.strip("/")

    # 1) Fetch index llms.txt
    index_url = f"{BASE}/{section}/llms.txt"
    try:
        results.append(_fetch_and_cache(index_url))
        index_md = (CACHE_ROOT / section / "llms.txt").read_text("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        results.append(
            FetchResult(
                url=index_url,
                status=f"error: {e}",
                bytes_written=0,
                path=_safe_path_from_url(index_url),
            )
        )
        return results

    # 2) Try llms-full.txt (nice for chat tools)
    full_url = f"{BASE}/{section}/llms-full.txt"
    try:
        results.append(_fetch_and_cache(full_url))
    except Exception:
        pass

    # 3) From llms.txt, pull endpoint .md docs (bounded)
    links = _extract_markdown_links(index_md)
    endpoint_urls = [u for u in links if _is_massive_docs_url(u) and _is_endpoint_md(u)]

    seen = set()
    endpoint_urls_deduped = []
    for u in endpoint_urls:
        if u not in seen:
            seen.add(u)
            endpoint_urls_deduped.append(u)

    endpoint_urls_deduped = endpoint_urls_deduped[:max_endpoints]

    for u in endpoint_urls_deduped:
        try:
            results.append(_fetch_and_cache(u))
        except Exception as e:  # noqa: BLE001
            results.append(FetchResult(url=u, status=f"error: {e}", bytes_written=0, path=_safe_path_from_url(u)))

    # 4) Build a single bundle file for this section
    try:
        bundle_path = CACHE_ROOT / section / "_bundle.md"
        bundle_path.parent.mkdir(parents=True, exist_ok=True)

        parts: List[str] = []
        parts.append("# Massive Docs Cache Bundle\n")
        parts.append(f"- Section: `{section}`\n")
        parts.append(f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        parts.append(f"- Source index: {index_url}\n\n")
        parts.append("---\n\n")
        parts.append("## Index (llms.txt)\n\n")
        parts.append(index_md)
        parts.append("\n\n---\n\n")

        for u in endpoint_urls_deduped:
            local = _safe_path_from_url(u)
            if local.exists():
                parts.append(f"## Endpoint: {u}\n\n")
                parts.append(local.read_text("utf-8", errors="replace"))
                parts.append("\n\n---\n\n")

        bundle_path.write_text("".join(parts), encoding="utf-8")
        results.append(FetchResult(url="(bundle)", status="ok", bytes_written=bundle_path.stat().st_size, path=bundle_path))
    except Exception as e:  # noqa: BLE001
        results.append(FetchResult(url="(bundle)", status=f"error: {e}", bytes_written=0, path=(CACHE_ROOT / section / "_bundle.md")))

    return results


def cmd_refresh(args: argparse.Namespace) -> int:
    sections = args.sections or DEFAULT_SECTIONS
    max_endpoints = int(args.max_endpoints)

    all_results: List[FetchResult] = []
    for s in sections:
        all_results.extend(refresh_section(s, max_endpoints=max_endpoints))

    ok = sum(1 for r in all_results if r.status == "ok")
    err = len(all_results) - ok

    print(f"[massive_docs] refresh done: ok={ok} err={err} cache_root={CACHE_ROOT.resolve()}")
    for r in all_results:
        if r.status != "ok":
            print(f"  ! {r.status} :: {r.url}")

    manifest = CACHE_ROOT / "_manifest.txt"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for r in all_results:
        lines.append(f"{r.status}\t{r.bytes_written}\t{r.url}\t{r.path.as_posix()}\n")
    manifest.write_text("".join(lines), encoding="utf-8")

    return 0 if err == 0 else 2


def cmd_list(_: argparse.Namespace) -> int:
    if not CACHE_ROOT.exists():
        print("[massive_docs] cache empty. Run refresh first.")
        return 1
    print(f"[massive_docs] cache root: {CACHE_ROOT.resolve()}")
    for p in sorted(CACHE_ROOT.rglob("*")):
        if p.is_file():
            rel = p.relative_to(CACHE_ROOT)
            print(rel.as_posix())
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    needle = args.query.strip()
    if not needle:
        print("search query required")
        return 1

    if not CACHE_ROOT.exists():
        print("[massive_docs] cache empty. Run refresh first.")
        return 1

    candidates = []
    for p in CACHE_ROOT.rglob("*"):
        if p.is_file() and (p.name.endswith(".md") or p.name.endswith(".txt")):
            candidates.append(p)

    hits = 0
    needle_l = needle.lower()
    for p in candidates:
        try:
            txt = p.read_text("utf-8", errors="replace")
        except Exception:
            continue
        if needle_l in txt.lower():
            hits += 1
            rel = p.relative_to(CACHE_ROOT).as_posix()
            print(f"\n---\nFILE: {rel}\n---")
            lines = txt.splitlines()
            for i, line in enumerate(lines):
                if needle_l in line.lower():
                    start = max(0, i - 2)
                    end = min(len(lines), i + 3)
                    for j in range(start, end):
                        prefix = ">" if j == i else " "
                        print(f"{prefix} {j+1:04d}: {lines[j]}")

    print(f"\n[massive_docs] search hits={hits} for query={needle!r}")
    return 0 if hits > 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cache Massive llms.txt docs locally for TNT")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_refresh = sub.add_parser("refresh", help="Refresh cache")
    p_refresh.add_argument("--sections", nargs="*", help="Sections under /docs (e.g. rest/options websocket)")
    p_refresh.add_argument(
        "--max-endpoints",
        type=int,
        default=DEFAULT_MAX_ENDPOINTS,
        help="Max endpoint .md files per section",
    )
    p_refresh.set_defaults(func=cmd_refresh)

    p_list = sub.add_parser("list", help="List cached files")
    p_list.set_defaults(func=cmd_list)

    p_search = sub.add_parser("search", help="Search cached docs")
    p_search.add_argument("query", help="Search string")
    p_search.set_defaults(func=cmd_search)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
