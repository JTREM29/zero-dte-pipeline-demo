from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import aiohttp


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


def _default_docs_base_url() -> str:
    # Prefer explicit docs base.
    v = (os.getenv("MASSIVE_DOCS_BASE_URL") or "").strip().rstrip("/")
    if v:
        return v

    # Fall back to API base (common in this repo).
    v = (os.getenv("MASSIVE_BASE_URL") or "").strip().rstrip("/")
    if v:
        return v

    # Conservative default: Massive docs are hosted on massive.com.
    return "https://massive.com"


@dataclass(frozen=True)
class _FetchSpec:
    kind: str
    url: str
    rel_save_path: Path


_MD_URL_RE = re.compile(r"(https?://[^\s\)\]<>\"']+\.md)\b", re.IGNORECASE)
_REL_MD_RE = re.compile(r"(?P<path>/docs/[^\s\)\]<>\"']+\.md)\b", re.IGNORECASE)


def _extract_md_urls(text: str, *, docs_base_url: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    for m in _MD_URL_RE.finditer(text or ""):
        u = m.group(1).strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    # Also handle relative /docs/... links.
    for m in _REL_MD_RE.finditer(text or ""):
        p = m.group("path").strip()
        if not p:
            continue
        u = urljoin(docs_base_url.rstrip("/") + "/", p.lstrip("/"))
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    return out


def _save_path_for_docs_url(out_dir: Path, url: str) -> Path:
    """Map a docs URL into docs/massive/<path after /docs/>."""

    parsed = urlparse(url)
    path = parsed.path or ""

    # Prefer stripping leading /docs/ so repo layout matches request.
    if "/docs/" in path:
        _, tail = path.split("/docs/", 1)
        rel = Path(tail.lstrip("/"))
        return out_dir / rel

    # Fallback: stash under _external/<host>/... to avoid collisions.
    host = parsed.netloc or "unknown-host"
    rel = Path("_external") / host / path.lstrip("/")
    return out_dir / rel


async def _fetch_text(session: aiohttp.ClientSession, url: str, *, timeout_s: float, retries: int) -> str:
    last_exc: Exception | None = None
    for attempt in range(max(1, retries + 1)):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
                if resp.status == 429 or 500 <= int(resp.status) <= 599:
                    raise RuntimeError(f"upstream status={resp.status}")
                if resp.status != 200:
                    return ""
                return await resp.text()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            # Simple backoff; keep bounded.
            await asyncio.sleep(min(1.5, 0.4 * (attempt + 1)))
    raise RuntimeError(f"fetch failed url={url} err={type(last_exc).__name__}")


def _with_cache_bust(url: str, token: str | None) -> str:
    if not token:
        return url
    try:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        # Use a stable param name to avoid collisions.
        query["_cb"] = str(token)
        return urlunparse(parsed._replace(query=urlencode(query)))
    except Exception:
        return url


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _build_index_specs(*, docs_base_url: str, out_dir: Path, only: set[str]) -> list[_FetchSpec]:
    base = docs_base_url.rstrip("/")

    def _want(kind: str) -> bool:
        return ("all" in only) or (kind in only)

    specs: list[_FetchSpec] = []

    # Always include the global REST llms index. It links to Partner docs (including Benzinga)
    # and keeps contract discovery robust.
    specs.append(
        _FetchSpec(
            kind="rest_root",
            url=f"{base}/docs/rest/llms.txt",
            rel_save_path=Path("rest/llms.txt"),
        )
    )

    if _want("stocks"):
        specs.append(
            _FetchSpec(
                kind="stocks",
                url=f"{base}/docs/rest/stocks/llms.txt",
                rel_save_path=Path("rest/stocks/llms.txt"),
            )
        )
    if _want("options"):
        specs.append(
            _FetchSpec(
                kind="options",
                url=f"{base}/docs/rest/options/llms.txt",
                rel_save_path=Path("rest/options/llms.txt"),
            )
        )
    if _want("websocket"):
        specs.append(
            _FetchSpec(
                kind="websocket",
                url=f"{base}/docs/websocket/llms.txt",
                rel_save_path=Path("websocket/llms.txt"),
            )
        )

    # Save under docs/massive/<...>
    return [_FetchSpec(kind=s.kind, url=s.url, rel_save_path=(out_dir / s.rel_save_path)) for s in specs]


async def main_async(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    p = argparse.ArgumentParser(description="Sync Massive docs (llms.txt + linked .md) into docs/massive/")
    p.add_argument("--base-url", default=_default_docs_base_url(), help="Docs base URL (default from MASSIVE_DOCS_BASE_URL/MASSIVE_BASE_URL)")
    p.add_argument("--out", default=str(_REPO_ROOT / "docs" / "massive"), help="Output root directory")
    p.add_argument(
        "--only",
        default="all",
        help="Comma list: all,stocks,options,websocket",
    )
    p.add_argument("--timeout", type=float, default=_env_float("MASSIVE_DOCS_TIMEOUT_SEC", 8.0), help="Per-request timeout seconds")
    p.add_argument("--retries", type=int, default=_env_int("MASSIVE_DOCS_RETRIES", 2), help="Retry count (max 2 recommended)")
    p.add_argument("--max-pages", type=int, default=_env_int("MASSIVE_DOCS_MAX_PAGES", 400), help="Safety cap for downloaded .md pages")
    p.add_argument(
        "--cache-bust",
        action="store_true",
        help="Append a cache-bust query param to docs requests (helps bypass CDN caching)",
    )
    args = p.parse_args(argv)

    docs_base = str(args.base_url or "").strip().rstrip("/")
    if not docs_base:
        print("base-url required")
        return 2

    out_dir = Path(str(args.out)).resolve()
    only = {s.strip().lower() for s in str(args.only or "all").split(",") if s.strip()}
    if not only:
        only = {"all"}

    # Public summary should list only these high-level families.
    public_index_set: set[str]
    if "all" in only:
        public_index_set = {"stocks", "options", "websocket"}
    else:
        public_index_set = {s for s in only if s in {"stocks", "options", "websocket"}}

    # Prepare fetch list.
    index_specs = _build_index_specs(docs_base_url=docs_base, out_dir=out_dir, only=only)

    timeout_s = max(2.0, float(args.timeout))
    retries = max(0, min(2, int(args.retries)))
    max_pages = max(1, min(5000, int(args.max_pages)))

    cache_bust = bool(args.cache_bust) or (os.getenv("MASSIVE_DOCS_CACHE_BUST", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
    cache_token = str(int(asyncio.get_running_loop().time() * 1000)) if cache_bust else None

    headers = {
        "Accept": "text/plain, text/markdown, */*",
        "User-Agent": "ZeroDTE-pipeline/massive-docs-sync",
        # Encourage intermediary caches to revalidate.
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    downloaded_indexes = 0
    index_kinds: list[str] = []
    discovered_pages: list[str] = []

    async with aiohttp.ClientSession(headers=headers) as session:
        for spec in index_specs:
            try:
                text = await _fetch_text(session, _with_cache_bust(spec.url, cache_token), timeout_s=timeout_s, retries=retries)
            except Exception as exc:  # noqa: BLE001
                print(f"index_fetch_failed kind={spec.kind} url={spec.url} err={type(exc).__name__}", file=sys.stderr)
                continue

            _atomic_write(spec.rel_save_path, text)
            if spec.kind in public_index_set:
                downloaded_indexes += 1
                if spec.kind and spec.kind not in index_kinds:
                    index_kinds.append(spec.kind)
            discovered_pages.extend(_extract_md_urls(text, docs_base_url=docs_base))

        # De-dupe while preserving order.
        uniq_pages: list[str] = []
        seen: set[str] = set()
        for u in discovered_pages:
            if u in seen:
                continue
            seen.add(u)
            uniq_pages.append(u)

        if len(uniq_pages) > max_pages:
            uniq_pages = uniq_pages[:max_pages]

        pages_fetched = 0
        pages_written = 0
        for u in uniq_pages:
            pages_fetched += 1
            save_path = _save_path_for_docs_url(out_dir, u)
            try:
                text = await _fetch_text(session, _with_cache_bust(u, cache_token), timeout_s=timeout_s, retries=retries)
            except Exception as exc:  # noqa: BLE001
                print(f"page_fetch_failed url={u} err={type(exc).__name__}", file=sys.stderr)
                continue
            if not text:
                continue
            _atomic_write(save_path, text)
            pages_written += 1

    elapsed_ms = int(max(0.0, (time.perf_counter() - started) * 1000.0))

    # Golden summary schema: single JSON line on stdout.
    summary = {
        "ok": bool(downloaded_indexes > 0),
        "base_url": docs_base,
        "indexes": list(index_kinds),
        "pages_fetched": int(len(uniq_pages)),
        "pages_written": int(pages_written),
        "skipped": int(max(0, int(len(uniq_pages)) - int(pages_written))),
        "elapsed_ms": int(elapsed_ms),
    }

    sys.stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if summary["ok"] else 2


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
