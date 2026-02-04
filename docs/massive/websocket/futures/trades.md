# WEBSOCKET
## Futures

### Trades

**Endpoint:** `WS /futures/T`

**Description:**

Stream tick-level trade data for futures tickers via WebSocket. Each message delivers key trade details (price, size, exchange, conditions, and timestamps) as they occur, enabling users to track market activity, power live dashboards, and inform rapid decision-making.

Use Cases: Live monitoring, algorithmic trading, market analysis, data visualization.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a future ticker or use * to subscribe to all future tickers. You can also use a comma separated list to subscribe to multiple future tickers. You can retrieve available future tickers from our [Futures Contracts API](https://massive.com/docs/rest/futures/contracts/contract-overview).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: T | The event type. |
| `sym` | string | The ticker symbol for the given future. |
| `p` | number | The trade price is quoted per unit of the underlying asset, with the total contract value determined by multiplying by the contract’s specific multiplier. |
| `s` | integer | The trade size shows the number of futures contracts actually exchanged. |
| `t` | integer | The timestamp in Unix MS. |
| `q` | integer | The sequence number represents the sequence in which message events happened. These are increasing and unique per ticker symbol, but will not always be sequential (e.g., 1, 2, 6, 9, 10, 11).  |

## Sample Response

```json
{
  "ev": "T",
  "sym": "ESZ4",
  "z": 3,
  "p": 606450,
  "s": 100,
  "t": 1734103628363,
  "q": 32599300
}
```
