"""Data connectors package with optional IQFeed exposure."""
from data import iqfeed_client

from zero_dte_pipeline.data_connectors.base import DataConnector, DataConnectorError
from zero_dte_pipeline.data_connectors.polygon import PolygonConnector
from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector

__all__ = [
    "DataConnector",
    "DataConnectorError",
    "PolygonConnector",
    "UnifiedDataConnector",
]

if iqfeed_client.IQFEED_ENABLED:
    IQFeedConnector = iqfeed_client.get_connector_class()
    __all__.append("IQFeedConnector")

