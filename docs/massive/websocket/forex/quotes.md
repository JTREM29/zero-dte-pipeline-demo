# WEBSOCKET
## Forex

### Quotes

**Endpoint:** `WS /forex/C`

**Description:**

Stream real-time Best Bid and Offer (BBO) quote data for specified Forex currency pairs via WebSocket. Each message provides the current bid/ask prices, sizes, and associated metadata as they update, enabling users to monitor evolving market conditions, guide trading decisions, and power responsive, data-driven applications.

Use Cases: Live monitoring, market analysis, trading decision support, dynamic interface updates.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a forex pair in the format {from}-{to} or use * to subscribe to all forex pairs. You can also use a comma separated list to subscribe to multiple forex pairs. You can retrieve active forex tickers from our [Forex Tickers API](https://massive.com/docs/rest/forex/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: C | The event type. |
| `p` | string | The current pair. |
| `x` | integer | The exchange ID. See <a href="https://massive.com/docs/rest/forex/market-operations/exchanges" alt="Exchanges">Exchanges</a> for Massive's mapping of exchange IDs. |
| `a` | number | The ask price. |
| `b` | number | The bid price. |
| `t` | integer | The Timestamp in Unix MS. |

## Sample Response

```json
{
  "ev": "C",
  "p": "USD/CNH",
  "x": "44",
  "a": 6.83366,
  "b": 6.83363,
  "t": 1536036818784
}
```
