# WEBSOCKET
## Options

### Trades

**Endpoint:** `WS /options/T`

**Description:**

Stream tick-level trade data for option contracts via WebSocket. Each message delivers key trade details (price, size, exchange, conditions, and timestamps) as they occur, enabling users to track market activity, power live dashboards, and inform rapid decision-making.

Use Cases: Live monitoring, algorithmic trading, market analysis, data visualization.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify an option contract or use * to subscribe to all option contracts. You can also use a comma separated list to subscribe to multiple option contracts. You can retrieve active options contracts from our [Options Contracts API](https://massive.com/docs/rest/options/contracts/all-contracts).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: T | The event type. |
| `sym` | string | The ticker symbol for the given option contract. |
| `x` | integer | The exchange ID. See <a target="_blank" href="https://massive.com/docs/rest/stocks/market-operations/exchanges" alt="Exchanges">Exchanges</a> for Massive's mapping of exchange IDs. |
| `p` | number | The price. |
| `s` | integer | The trade size. |
| `c` | array[integer] | The trade conditions |
| `t` | integer | The Timestamp in Unix MS. |
| `q` | integer | The sequence number represents the sequence in which trade events happened. These are increasing and unique per ticker symbol, but will not always be sequential (e.g., 1, 2, 6, 9, 10, 11). |

## Sample Response

```json
{
  "ev": "T",
  "sym": "O:AMC210827C00037000",
  "x": 65,
  "p": 1.54,
  "s": 1,
  "c": [
    233
  ],
  "t": 1629820676333,
  "q": 651921857
}
```
