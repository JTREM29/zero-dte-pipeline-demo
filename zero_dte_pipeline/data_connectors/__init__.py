"""Data connectors package with optional IQFeed exposure.

IQFeed is hard-disabled by default. To enable it (explicitly), set:

    TNT_ENABLE_IQFEED=1
"""

import os

from zero_dte_pipeline.data_connectors.base import DataConnector, DataConnectorError
from zero_dte_pipeline.data_connectors.polygon import PolygonConnector
from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector

__all__ = [
    "DataConnector",
    "DataConnectorError",
    "PolygonConnector",
    "UnifiedDataConnector",
]

# IQFeed is opt-in and must not be imported unless explicitly enabled.
if (os.getenv("TNT_ENABLE_IQFEED", "0") or "0").strip() == "1":
    from zero_dte_pipeline.data_connectors.iqfeed import IQFeedConnector  # noqa: F401

    __all__.append("IQFeedConnector")

