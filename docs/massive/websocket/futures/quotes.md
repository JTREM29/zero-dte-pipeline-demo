# WEBSOCKET
## Futures

### Quotes

**Endpoint:** `WS /futures/Q`

**Description:**

Stream BBO (Best Bid and Offer) quote data for futures tickers via WebSocket. Each message provides the current best bid/ask prices, sizes, and related metadata as they update, allowing users to monitor evolving market conditions, inform trading decisions, and maintain responsive, data-driven applications.

Use Cases: Live monitoring, market analysis, trading decision support, dynamic interface updates.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a future ticker or use * to subscribe to all future tickers. You can also use a comma separated list to subscribe to multiple future tickers. You can retrieve available future tickers from our [Futures Contracts API](https://massive.com/docs/rest/futures/contracts/contract-overview).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: Q | The event type. |
| `sym` | string | The ticker symbol for the given future. |
| `bp` | number | The bid price is expressed per unit of the underlying asset, and you apply the contract multiplier to get the full contract value. |
| `bs` | integer | The quote size represents the number of futures contracts available at the given bid price. |
| `bt` | integer | The timestamp when the bid was submitted to the exchange. |
| `ap` | number | The ask price is expressed per unit of the underlying asset, and you apply the contract multiplier to get the full contract value. |
| `as` | integer | The quote size represents the number of futures contracts available at the given ask price. |
| `at` | integer | The timestamp when the ask was submitted to the exchange. |
| `t` | integer | The timestamp in Unix MS. |

## Sample Response

```json
{
  "ev": "Q",
  "sym": "ESZ4",
  "bp": 114.125,
  "bs": 100,
  "bt": 1734103628360,
  "ap": 114.128,
  "as": 160,
  "at": 1734103628350,
  "t": 1536036818784
}
```
