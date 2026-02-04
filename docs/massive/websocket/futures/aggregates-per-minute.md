# WEBSOCKET
## Futures

### Aggregates (Per Minute)

**Endpoint:** `WS /futures/AM`

**Description:**

Stream minute-by-minute aggregated OHLC (Open, High, Low, Close) and volume data for a specified futures contract ticker via WebSocket. Aggregates are continuously updated in Central Time (CT) and are constructed from all trades occurring within each defined aggregation window. If no trades occur during the window, no aggregate bar is emitted, indicating a period of inactivity. The response includes key metrics such as open, high, low, close, trade volume, and the start and end timestamps for each window.

Use Cases: Real-time monitoring, dynamic charting, intraday strategy development, automated trading.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a future ticker or use * to subscribe to all future tickers. You can also use a comma separated list to subscribe to multiple future tickers. You can retrieve available future tickers from our [Futures Contracts API](https://massive.com/docs/rest/futures/contracts/contract-overview).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: AM | The event type. |
| `sym` | string | The ticker symbol for the given future. |
| `v` | number | The tick volume. |
| `dv` | number | The total US dollar value of shares traded within the aggregate window. |
| `o` | number | The opening tick price for this aggregate window. |
| `c` | number | The closing tick price for this aggregate window. |
| `h` | number | The highest tick price for this aggregate window. |
| `l` | number | The lowest tick price for this aggregate window. |
| `n` | number | The total number of transactions that occurred within the aggregate window |
| `s` | integer | The start timestamp of this aggregate window in Unix Milliseconds. |
| `e` | integer | The end timestamp of this aggregate window in Unix Milliseconds. |

## Sample Response

```json
{
  "ev": "AM",
  "sym": "6CH5",
  "v": 91,
  "dv": 1353.1,
  "o": 6994.5,
  "c": 6995,
  "h": 6995,
  "l": 6994.5,
  "n": 10,
  "s": 1751933700000,
  "e": 1751933760000
}
```
