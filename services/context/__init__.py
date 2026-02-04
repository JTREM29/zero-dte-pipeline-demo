"""Context snapshot system.

A 'context snapshot' is a small, normalized Redis JSON blob intended to be the
single source of truth for downstream consumers (gating, previews, status UIs,
trigger-time context line).

Keys (v1):
- ctx:market
- ctx:sym:{SYM}
"""
