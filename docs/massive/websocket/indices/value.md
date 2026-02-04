# WEBSOCKET
## Indices

### Value

**Endpoint:** `WS /indices/V`

**Description:**

Stream real-time index value updates for specified index tickers via WebSocket. Each message provides the index’s current value and timestamp, allowing users to monitor fluctuations in key market benchmarks or sectors throughout the trading session. This continuous feed supports live market analysis, trend identification, and integration of index-based signals into trading strategies and research.

Use Cases: Market analysis, trend detection, portfolio benchmarking, trading strategy refinement.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify an index ticker using "I:" prefix or use * to subscribe to all index tickers. You can also use a comma separated list to subscribe to multiple index tickers. You can retrieve available index tickers from our [Index Tickers API](https://massive.com/docs/rest/indices/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: V | The event type. |
| `val` | number | The value of the index. |
| `T` | string | The assigned ticker of the index. |
| `t` | integer | The Timestamp in Unix MS. |

## Sample Response

```json
{
  "ev": "V",
  "val": 3988.5,
  "T": "I:SPX",
  "t": 1678220098130
}
```
