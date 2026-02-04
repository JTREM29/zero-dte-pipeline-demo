# WEBSOCKET
## Stocks

### Aggregates (Per Minute)

**Endpoint:** `WS /stocks/AM`

**Description:**

Stream minute-by-minute aggregated OHLC (Open, High, Low, Close) and volume data for specified tickers via WebSocket. These aggregates are updated continuously in Eastern Time (ET) and cover pre-market, regular, and after-hours sessions. Each bar is constructed solely from qualifying trades that meet specific conditions; if no eligible trades occur within a given minute, no bar is emitted. By providing a steady flow of aggregate bars, this endpoint enables users to track intraday price movements, refine trading strategies, and power live data visualizations.

Use Cases: Real-time monitoring, dynamic charting, intraday strategy development, automated trading.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a stock ticker or use * to subscribe to all stock tickers. You can also use a comma separated list to subscribe to multiple stock tickers. You can retrieve available stock tickers from our [Stock Tickers API](https://massive.com/docs/rest/stocks/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: AM | The event type. |
| `sym` | string | The ticker symbol for the given stock. |
| `v` | integer | The tick volume. |
| `av` | integer | Today's accumulated volume. |
| `op` | number | Today's official opening price. |
| `vw` | number | The tick's volume weighted average price. |
| `o` | number | The opening tick price for this aggregate window. |
| `c` | number | The closing tick price for this aggregate window. |
| `h` | number | The highest tick price for this aggregate window. |
| `l` | number | The lowest tick price for this aggregate window. |
| `a` | number | Today's volume weighted average price. |
| `z` | integer | The average trade size for this aggregate window. |
| `s` | integer | The start timestamp of this aggregate window in Unix Milliseconds. |
| `e` | integer | The end timestamp of this aggregate window in Unix Milliseconds. |
| `otc` | boolean | Whether or not this aggregate is for an OTC ticker. This field will be left off if false. |

## Sample Response

```json
{
  "ev": "AM",
  "sym": "GTE",
  "v": 4110,
  "av": 9470157,
  "op": 0.4372,
  "vw": 0.4488,
  "o": 0.4488,
  "c": 0.4486,
  "h": 0.4489,
  "l": 0.4486,
  "a": 0.4352,
  "z": 685,
  "s": 1610144640000,
  "e": 1610144700000
}
```
