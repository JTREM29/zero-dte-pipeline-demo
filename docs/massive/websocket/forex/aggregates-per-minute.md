# WEBSOCKET
## Forex

### Aggregates (Per Minute)

**Endpoint:** `WS /forex/CA`

**Description:**

Stream minute-by-minute aggregated OHLC (Open, High, Low, Close) and volume data for a specified Forex currency pair via WebSocket. These aggregates update continuously in Eastern Time (ET) and are derived from the best bid/offer quotes rather than executed trades. If no new quotes occur within a given minute, no bar is emitted, transparently indicating a period without market updates. By providing a continuous feed of updated market snapshots, this endpoint supports intraday analysis, dynamic charting, and the refinement of real-time Forex trading strategies.

Use Cases: Real-time monitoring, dynamic charting, intraday strategy development, market research.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a forex pair in the format {from}-{to} or use * to subscribe to all forex pairs. You can also use a comma separated list to subscribe to multiple forex pairs. You can retrieve active forex tickers from our [Forex Tickers API](https://massive.com/docs/rest/forex/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: CA | The event type. |
| `pair` | string | The currency pair. |
| `o` | number | The open price for this aggregate window. |
| `c` | number | The close price for this aggregate window. |
| `h` | number | The high price for this aggregate window. |
| `l` | number | The low price for this aggregate window. |
| `v` | integer | The volume of trades during this aggregate window. |
| `s` | integer | The start timestamp of this aggregate window in Unix Milliseconds. |
| `e` | integer | The end timestamp of this aggregate window in Unix Milliseconds. |

## Sample Response

```json
{
  "ev": "CA",
  "pair": "USD/EUR",
  "o": 0.8687,
  "c": 0.86889,
  "h": 0.86889,
  "l": 0.8686,
  "v": 20,
  "s": 1539145740000
}
```
