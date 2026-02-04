# WEBSOCKET
## Options

### Aggregates (Per Second)

**Endpoint:** `WS /options/A`

**Description:**

Stream second-by-second aggregated OHLC (Open, High, Low, Close) and volume data for a specified options contract via WebSocket. These aggregates are updated continuously in Eastern Time (ET). Each bar is constructed solely from qualifying trades that meet specific conditions; if no eligible trades occur within a given minute, no bar is emitted. By delivering an ongoing feed of updated market snapshots, this endpoint enables users to closely monitor intraday price movements, enhance trading strategies, and support live data visualizations in the options market.

Use Cases: Real-time monitoring, dynamic charting, intraday strategy development, automated trading.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify an option contract or use * to subscribe to all option contracts. You can also use a comma separated list to subscribe to multiple option contracts. You can retrieve active options contracts from our [Options Contracts API](https://massive.com/docs/rest/options/contracts/all-contracts).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: A | The event type. |
| `sym` | string | The ticker symbol for the given option contract. |
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

## Sample Response

```json
{
  "ev": "AM",
  "sym": "O:ONEM220121C00025000",
  "v": 2,
  "av": 8,
  "op": 2.2,
  "vw": 2.05,
  "o": 2.05,
  "c": 2.05,
  "h": 2.05,
  "l": 2.05,
  "a": 2.1312,
  "z": 2,
  "s": 1632419640000,
  "e": 1632419700000
}
```
