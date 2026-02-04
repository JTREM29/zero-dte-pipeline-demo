# WEBSOCKET
## Crypto

### Trades

**Endpoint:** `WS /crypto/XT`

**Description:**

Stream trade data for crypto pairs via WebSocket. Each message delivers key trade details (price, size, exchange, conditions, and timestamps) as they occur, enabling users to track market activity, power live dashboards, and inform rapid decision-making.

Use Cases: Live monitoring, algorithmic trading, market analysis, data visualization.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a crypto pair in the format {from}-{to} or use * to subscribe to all crypto pairs. You can also use a comma separated list to subscribe to multiple crypto pairs. You can retrieve active crypto tickers from our [Crypto Tickers API](https://massive.com/docs/rest/crypto/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: XT | The event type. |
| `pair` | string | The crypto pair. |
| `p` | number | The price. |
| `t` | integer | The Timestamp in Unix MS. |
| `s` | number | The size. |
| `c` | array[integer] | The conditions. 0 (or empty array): empty 1: sellside 2: buyside  |
| `i` | integer | The ID of the trade (optional). |
| `x` | integer | The crypto exchange ID.  See <a target="_blank" href="https://massive.com/docs/rest/crypto/market-operations/exchanges">Exchanges</a> for a list of exchanges and their IDs.  |
| `r` | integer | The timestamp that the tick was received by Massive. |

## Sample Response

```json
{
  "ev": "XT",
  "pair": "BTC-USD",
  "p": 33021.9,
  "t": 1610462007425,
  "s": 0.01616617,
  "c": [
    2
  ],
  "i": 14272084,
  "x": 3,
  "r": 1610462007576
}
```
