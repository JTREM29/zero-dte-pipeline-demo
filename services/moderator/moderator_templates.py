from __future__ import annotations

from dataclasses import dataclass

from services.moderator.moderator_policy import ModeratorEnv, TemplateId


@dataclass(frozen=True)
class RenderedModeratorReply:
    text: str


def render_reply(*, template_id: TemplateId, env: ModeratorEnv) -> RenderedModeratorReply:
    """Deterministic, non-improvised reply templates.

    Keep these strings stable; tests golden-snapshot them.
    """

    owner_line = ""
    if env.owner_available:
        owner_line = "\n\nIf you need a second set of eyes, tag the owner."

    if template_id == TemplateId.NO_ENTRIES:
        return RenderedModeratorReply(
            text=(
                "I can’t provide trade entries/strikes/expirations.\n"
                "Safer next step: share your thesis + level(s) you’re watching, and I’ll help you frame confirmation/invalidations."
                + owner_line
            )
        )

    if template_id == TemplateId.NO_SIZING:
        return RenderedModeratorReply(
            text=(
                "I can’t provide sizing or ‘how many contracts’.\n"
                "Safer next step: define max loss $ and the invalidation level; size should be derived from that." + owner_line
            )
        )

    if template_id == TemplateId.NO_PREDICTIONS:
        return RenderedModeratorReply(
            text=(
                "I can’t predict direction/targets.\n"
                "Safer next step: focus on what would confirm vs. invalidate the move (levels + structure)." + owner_line
            )
        )

    if template_id == TemplateId.NO_EXITS:
        return RenderedModeratorReply(
            text=(
                "I can’t give exits/stop-loss/take-profit instructions.\n"
                "Safer next step: define the invalidation first; if price violates it, stand down." + owner_line
            )
        )

    if template_id == TemplateId.HELP:
        return RenderedModeratorReply(
            text=(
                "Ask about market context (VIX/futures/open), or request a ticker card with a symbol (e.g., ‘SPY’).\n"
                "I won’t give entries, sizing, or predictions."
            )
        )

    if template_id == TemplateId.GREETING:
        return RenderedModeratorReply(text="Hi. Ask a symbol + what you want to know (e.g., ‘SPY — market context’).")

    if template_id == TemplateId.MARKET_CONTEXT:
        return RenderedModeratorReply(
            text=(
                "I can help with market context (risk-on/off, volatility regime, key levels), not trade entries.\n"
                "Ask: ‘What’s the market context right now?’ or ‘How do the futures look?’"
            )
        )

    if template_id == TemplateId.FUTURES:
        return RenderedModeratorReply(text="Ask: ‘How do the futures look?’ and I’ll summarize the current futures context when fresh.")

    return RenderedModeratorReply(text="I can help with context and risk framing, not trade instructions.")
