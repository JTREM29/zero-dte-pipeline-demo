# WEBSOCKET
## Crypto

### Aggregates (Per Second)

**Endpoint:** `WS /crypto/XAS`

**Description:**

Stream second-by-second aggregated OHLC (Open, High, Low, Close) and volume data for a specified cryptocurrency pair via WebSocket. These aggregates update continuously in Coordinated Universal Time (UTC). If no trades occur within a given minute, no bar is emitted, transparently indicating a period without trading activity. This endpoint provides a live feed of aggregated bars, enabling users to monitor intraday price movements, refine trading strategies, and power real-time crypto market visualizations.

Use Cases: Real-time monitoring, dynamic charting, intraday strategy development, market research.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a crypto pair in the format {from}-{to} or use * to subscribe to all crypto pairs. You can also use a comma separated list to subscribe to multiple crypto pairs. You can retrieve active crypto tickers from our [Crypto Tickers API](https://massive.com/docs/rest/crypto/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: XAS | The event type. |
| `pair` | string | The crypto pair. |
| `o` | number | The open price for this aggregate window. |
| `c` | number | The close price for this aggregate window. |
| `h` | number | The high price for this aggregate window. |
| `l` | number | The low price for this aggregate window. |
| `v` | integer | The volume of trades during this aggregate window. |
| `s` | integer | The start timestamp of this aggregate window in Unix Milliseconds. |
| `e` | integer | The end timestamp of this aggregate window in Unix Milliseconds. |
| `vw` | number | The volume weighted average price. |
| `z` | integer | The average trade size for this aggregate window. |

## Sample Response

```json
{
  "ev": "XAS",
  "pair": "BCD-USD",
  "v": 951.6112,
  "vw": 0.7756,
  "z": 73,
  "o": 0.772,
  "c": 0.784,
  "h": 0.784,
  "l": 0.771,
  "s": 1610463240000,
  "e": 1610463300000
}
```
