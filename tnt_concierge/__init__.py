"""TNT Concierge helpers.

This package contains deterministic, fast message templates that feel "AI" without
calling an LLM.
"""

from .templates import (  # noqa: F401
    ConciergeContext,
    LockState,
    TemplateId,
    concierge_reply,
    pick_template,
    render_message,
)

from .engine import schedule_concierge_nudge  # noqa: F401
from .throttle import allow_nudge  # noqa: F401
