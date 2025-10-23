"""Strategy definitions and harnesses.

Import concrete strategy modules so their @register_strategy decorators execute
on package import. This enables dynamic lookup via get_strategy without each
caller needing to import every module explicitly.
"""

# Side-effect imports (registration)
from . import simple_intraday_spx  # noqa: F401
from . import odte_direction  # noqa: F401
from . import odte_blended  # noqa: F401
from . import odte_composite  # noqa: F401
from . import lottos_scalper  # noqa: F401

__all__ = [
	"simple_intraday_spx",
	"odte_direction",
	"odte_blended",
	"odte_composite",
	"lottos_scalper",
]
