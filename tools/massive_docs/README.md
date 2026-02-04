# Massive Docs Cache (llms.txt)

This folder caches Massive API documentation exposed via `llms.txt` into local markdown/text files.
Purpose: keep AI coding assistants grounded in the exact API contract (params/schemas) without HTML scraping.

## Refresh docs

From repo root:

- Refresh default sections:
  - `python tools/massive_docs/massive_docs.py refresh`
- Refresh only what TNT cares about:
  - `python tools/massive_docs/massive_docs.py refresh --sections rest/options rest/stocks websocket --max-endpoints 200`

## Search

- `python tools/massive_docs/massive_docs.py search "expected move"`

## Where to point your agent

Use the generated bundle files:

- `tools/massive_docs/cache/rest/options/_bundle.md`
- `tools/massive_docs/cache/rest/stocks/_bundle.md`
- `tools/massive_docs/cache/websocket/_bundle.md`

## Notes

- Override docs host (if you use a proxy/staging domain): set `MASSIVE_DOCS_BASE_URL` to something like `https://massive.com`.
- Cache files live under `tools/massive_docs/cache/`.
