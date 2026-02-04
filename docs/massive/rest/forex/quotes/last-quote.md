# REST
## Forex

### Last Quote

**Endpoint:** `GET /v1/last_quote/currencies/{from}/{to}`

**Description:**

Retrieve the most recent quote for a specified forex currency pair, including bid, ask, exchange, and timestamp. This endpoint provides up-to-date pricing data to inform currency trading strategies, market analysis, and application development.

Use Cases: Real-time forex monitoring, algorithmic trading, analytical insights, application development.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `from` | string | Yes | The "from" symbol of the pair. |
| `to` | string | Yes | The "to" symbol of the pair. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `last` | object | Contains the requested quote data for the specified forex currency pair. |
| `last.ask` | number | The ask price. |
| `last.bid` | number | The bid price. |
| `last.exchange` | integer | The exchange ID. See <a href="https://massive.com/docs/rest/forex/market-operations/exchanges" alt="Exchanges">Exchanges</a> for Massive's mapping of exchange IDs. |
| `last.timestamp` | integer | The Unix millisecond timestamp. |
| `request_id` | string | A request id assigned by the server. |
| `status` | string | The status of this request's response. |
| `symbol` | string | The symbol pair that was evaluated from the request. |

## Sample Response

```json
{
  "last": {
    "ask": 0.73124,
    "bid": 0.73122,
    "exchange": 48,
    "timestamp": 1605557756000
  },
  "request_id": "a73a29dbcab4613eeaf48583d3baacf0",
  "status": "success",
  "symbol": "AUD/USD"
}
```
