"""Locked messaging contract for TNT AI Trade Candidates.

This module centralizes the non-negotiable, user-facing sentences so they cannot drift
across coach/guard/autopost.

See docs/AI_TRADE_CANDIDATES_CONTRACT.md.
"""

from __future__ import annotations

# Canonical definition (internal – never change).
AI_TRADE_CANDIDATES_CANONICAL_DEFINITION = (
    "AI Trade Candidates are option structures that statistically fit the current market regime, "
    "volatility state, and positioning — not instructions to trade."
)

# Explicit boundary (required, verbatim).
AI_TRADE_CANDIDATES_BOUNDARY_SENTENCE = "This is a candidate, not a command."

# No-candidate messaging (required, verbatim).
AI_TRADE_CANDIDATES_NO_CANDIDATE_SENTENCE = (
    "There is no AI trade candidate right now because conditions do not offer a statistical edge."
)

# Misinterpretation correction (required, verbatim).
AI_TRADE_CANDIDATES_MISINTERPRETATION_CORRECTION_SENTENCE = (
    "This wasn’t meant as a signal — it’s a structure that fits conditions if you choose to trade."
)
