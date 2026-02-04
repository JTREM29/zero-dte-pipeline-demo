# WEBSOCKET
## Crypto

### Quotes

**Endpoint:** `WS /crypto/XQ`

**Description:**

Stream quote data for specified cryptocurrency pairs via WebSocket. Each message delivers current bid/ask prices, sizes, and relevant metadata from multiple exchanges as they update, allowing users to monitor evolving market conditions, guide trading decisions, and support responsive, data-driven applications.

Use Cases: Live monitoring, market analysis, trading decision support, dynamic interface updates.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a crypto pair in the format {from}-{to} or use * to subscribe to all crypto pairs. You can also use a comma separated list to subscribe to multiple crypto pairs. You can retrieve active crypto tickers from our [Crypto Tickers API](https://massive.com/docs/rest/crypto/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: XQ | The event type. |
| `pair` | string | The crypto pair. |
| `bp` | number | The bid price. |
| `bs` | number | The bid size. |
| `ap` | number | The ask price. |
| `as` | number | The ask size. |
| `t` | integer | The Timestamp in Unix MS. |
| `x` | integer | The crypto exchange ID.  See <a target="_blank" href="https://massive.com/docs/rest/crypto/market-operations/exchanges">Exchanges</a> for a list of exchanges and their IDs.  |
| `r` | integer | The timestamp that the tick was received by Massive. |

## Sample Response

```json
{
  "ev": "XQ",
  "pair": "BTC-USD",
  "bp": 33052.79,
  "bs": 0.48,
  "ap": 33073.19,
  "as": 0.601,
  "t": 1610462411115,
  "x": 1,
  "r": 1610462411128
}
```
