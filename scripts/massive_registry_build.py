from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_REPO_ROOT = Path(__file__).resolve().parents[1]


_HTTP_LINE_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s\)\]]+)", re.IGNORECASE)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(x: object) -> str:
    return str(x).strip() if x is not None else ""


def _default_docs_base_url() -> str:
    v = (os.getenv("MASSIVE_DOCS_BASE_URL") or "").strip().rstrip("/")
    if v:
        return v
    v = (os.getenv("MASSIVE_BASE_URL") or "").strip().rstrip("/")
    if v:
        return v
    return "https://massive.com"


def _iter_md_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.md"):
        # Skip registry output location if someone puts it under .md.
        if p.name.lower().endswith("registry.md"):
            continue
        yield p


def _extract_title(md: str, fallback: str) -> str:
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip() or fallback
    return fallback


def _extract_method_path(md: str) -> tuple[str | None, str | None]:
    # Prefer explicit "GET /path" style lines.
    for line in (md or "").splitlines():
        m = _HTTP_LINE_RE.search(line)
        if not m:
            continue
        method = m.group(1).upper()
        path = m.group(2).strip()
        # Docs often wrap endpoint strings in backticks.
        path = path.strip("`")
        path = path.rstrip("`.,:;")
        if path.startswith("http"):
            continue
        return method, path

    # Fallback: find a path-like token and infer GET.
    # This is intentionally conservative; contract tests will ensure core endpoints show up.
    for line in (md or "").splitlines():
        s = line.strip()
        if "/benzinga/" in s and "/" in s:
            m = re.search(r"(/benzinga/[^\s\)\]]+)", s)
            if m:
                return "GET", m.group(1).strip()

    return None, None


def _classify_missing_method_path(md: str) -> str:
    """Best-effort reason for why an endpoint couldn't be extracted."""

    text = md or ""
    if not _HTTP_LINE_RE.search(text):
        return "no_endpoint_block"

    # Extremely rare with current regex, but keep for forward compatibility.
    # If there is an HTTP-ish line but our extractor didn't return a tuple.
    return "unparsed"


def _parse_markdown_table_params(md: str) -> tuple[list[str], list[str]]:
    """Return (all_params, required_params) using a cheap markdown-table heuristic."""

    lines = (md or "").splitlines()
    all_params: list[str] = []
    required: list[str] = []

    def _cell(row: str, idx: int) -> str:
        parts = [c.strip() for c in row.strip().strip("|").split("|")]
        return parts[idx] if 0 <= idx < len(parts) else ""

    for i in range(len(lines) - 2):
        h = lines[i].strip().lower()
        sep = lines[i + 1].strip()
        if "|" not in h or "---" not in sep:
            continue
        if not ("param" in h or "parameter" in h or "name" in h):
            continue

        # Determine column indices.
        header_cells = [c.strip().lower() for c in lines[i].strip().strip("|").split("|")]
        name_idx = 0
        req_idx = None
        for j, c in enumerate(header_cells):
            if c in {"param", "parameter", "name"}:
                name_idx = j
            if "required" in c:
                req_idx = j

        # Consume rows until a non-table line.
        for j in range(i + 2, len(lines)):
            row = lines[j].strip()
            if not row.startswith("|"):
                break
            name = _cell(row, name_idx)
            name = name.strip("` ").strip()
            if not name:
                continue
            if name not in all_params:
                all_params.append(name)

            if req_idx is not None:
                req = _cell(row, req_idx).lower()
                if any(tok in req for tok in ("yes", "true", "required")):
                    if name not in required:
                        required.append(name)

        # Only parse the first detected table.
        break

    return all_params, required


def _extract_inline_param_hints(md: str) -> list[str]:
    # Common pattern: `ticker`, `tickers`, `published_since` etc.
    # We keep this list small and let table parsing dominate.
    hints: set[str] = set()
    for m in re.finditer(r"`([a-zA-Z_][a-zA-Z0-9_]*)`", md or ""):
        tok = m.group(1)
        if tok and len(tok) <= 40:
            hints.add(tok)
    return sorted(hints)


@dataclass(frozen=True)
class RegistryEntry:
    name: str
    url: str
    method: str
    path: str
    required_params: list[str]
    params: list[str]
    notes: str

    def to_jsonable(self) -> dict[str, object]:
        return {
            "name": self.name,
            "url": self.url,
            "method": self.method,
            "path": self.path,
            "required_params": list(self.required_params),
            "params": list(self.params),
            "notes": self.notes,
        }


def main(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    p = argparse.ArgumentParser(description="Build docs/massive/registry.json from synced Massive markdown")
    p.add_argument("--docs-root", default=str(_REPO_ROOT / "docs" / "massive"), help="Root dir containing synced docs")
    p.add_argument("--docs-base-url", default=_default_docs_base_url(), help="Docs base url (for registry urls)")
    p.add_argument("--out", default=str(_REPO_ROOT / "docs" / "massive" / "registry.json"), help="Output registry path")
    args = p.parse_args(argv)

    docs_root = Path(str(args.docs_root)).resolve()
    if not docs_root.exists():
        raise SystemExit(f"docs root not found: {docs_root}")

    base = str(args.docs_base_url or "").strip().rstrip("/")
    if not base:
        base = "https://massive.com"

    md_files_scanned = 0
    missing_method_or_path = 0
    missing_samples: list[str] = []
    missing_kind_counts: dict[str, int] = {}
    entries: list[RegistryEntry] = []

    for md_path in sorted(_iter_md_files(docs_root)):
        md_files_scanned += 1
        rel = md_path.relative_to(docs_root).as_posix()
        # registry is for endpoint pages; ignore llms indexes.
        if rel.lower().endswith("llms.txt"):
            continue

        md = md_path.read_text(encoding="utf-8", errors="replace")
        name = _extract_title(md, fallback=md_path.stem)
        method, path = _extract_method_path(md)
        if not method or not path:
            missing_method_or_path += 1

            kind = _classify_missing_method_path(md)
            missing_kind_counts[kind] = int(missing_kind_counts.get(kind, 0)) + 1

            if len(missing_samples) < 5:
                # Prefer repo-relative paths so copy/paste is useful.
                try:
                    sample_rel = md_path.resolve().relative_to(_REPO_ROOT).as_posix()
                except Exception:
                    sample_rel = md_path.as_posix()
                missing_samples.append(sample_rel)
            continue

        params_all, params_required = _parse_markdown_table_params(md)
        if not params_all:
            # Add light fallback based on backticked words.
            hints = _extract_inline_param_hints(md)
            params_all = hints[:25]

        # Construct URL pointing at docs.
        url = f"{base}/docs/{rel}"

        entries.append(
            RegistryEntry(
                name=name,
                url=url,
                method=str(method).upper(),
                path=str(path),
                required_params=list(params_required),
                params=list(params_all),
                notes="auto-generated; parsing is best-effort",
            )
        )

    out_obj: dict[str, object] = {
        "generated_utc": _now_utc_iso(),
        "docs_base_url": base,
        "source_dir": str(docs_root),
        "entries": [e.to_jsonable() for e in sorted(entries, key=lambda x: (x.method, x.path))],
    }

    out_path = Path(str(args.out)).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(out_obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out_path)

    elapsed_ms = int(max(0.0, (time.perf_counter() - started) * 1000.0))
    try:
        registry_rel = out_path.relative_to(_REPO_ROOT).as_posix()
    except Exception:
        registry_rel = str(out_path)

    summary = {
        "ok": True,
        "md_files_scanned": int(md_files_scanned),
        "entries_written": int(len(entries)),
        "missing_method_or_path": int(missing_method_or_path),
        "missing_samples": list(missing_samples),
        "missing_kind_counts": dict(sorted(missing_kind_counts.items(), key=lambda kv: kv[0])),
        "registry_path": str(registry_rel),
        "elapsed_ms": int(elapsed_ms),
    }
    sys.stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
