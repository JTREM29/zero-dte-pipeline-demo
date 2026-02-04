# REST
## Forex

### Currency Conversion

**Endpoint:** `GET /v1/conversion/{from}/{to}`

**Description:**

Retrieve real-time currency conversion rates between any two supported currencies. This endpoint provides the most recent bid/ask quotes and calculates the converted amount based on the current market rate, enabling users to quickly and accurately convert values in both directions (e.g., USD to CAD or CAD to USD).

Use Cases: Cross-border transactions, currency hedging, travel expense planning, dynamic pricing.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `from` | string | Yes | The "from" symbol of the pair. |
| `to` | string | Yes | The "to" symbol of the pair. |
| `amount` | number | No | The amount to convert, with a decimal. |
| `precision` | integer | No | The decimal precision of the conversion. Defaults to 2 which is 2 decimal places accuracy. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `converted` | number | The result of the conversion. |
| `from` | string | The "from" currency symbol. |
| `initialAmount` | number | The amount to convert. |
| `last` | object | Contains the requested quote data for the specified forex currency pair. |
| `last.ask` | number | The ask price. |
| `last.bid` | number | The bid price. |
| `last.exchange` | integer | The exchange ID. See <a href="https://massive.com/docs/rest/forex/market-operations/exchanges" alt="Exchanges">Exchanges</a> for Massive's mapping of exchange IDs. |
| `last.timestamp` | integer | The Unix millisecond timestamp. |
| `request_id` | string | A request id assigned by the server. |
| `status` | string | The status of this request's response. |
| `symbol` | string | The symbol pair that was evaluated from the request. |
| `to` | string | The "to" currency symbol. |

## Sample Response

```json
{
  "converted": 73.14,
  "from": "AUD",
  "initialAmount": 100,
  "last": {
    "ask": 1.3673344,
    "bid": 1.3672596,
    "exchange": 48,
    "timestamp": 1605555313000
  },
  "request_id": "a73a29dbcab4613eeaf48583d3baacf0",
  "status": "success",
  "symbol": "AUD/USD",
  "to": "USD"
}
```
