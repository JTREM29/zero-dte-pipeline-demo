# WEBSOCKET
## Crypto

### Fair Market Value

**Endpoint:** `WS /business/crypto/FMV`

**Description:**

Stream real-time Fair Market Value (FMV) data for a specified cryptocurrency pair via WebSocket. This proprietary metric, available exclusively to Business plan users, provides an algorithmically derived, real-time estimate of the crypto pair’s fair market price. By delivering accurate, continuous valuation data, this feed supports more informed trading decisions, enhanced analytics, and improved risk management.

Use Cases: Pricing strategies, algorithmic modeling, risk assessment, investor decision-making.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a crypto pair in the format {from}-{to} or use * to subscribe to all crypto pairs. You can also use a comma separated list to subscribe to multiple crypto pairs. You can retrieve active crypto tickers from our [Crypto Tickers API](https://massive.com/docs/rest/crypto/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: FMV | The event type. |
| `fmv` | number | Fair market value is only available on Business plans. It is our proprietary algorithm to generate a real-time, accurate, fair market value of a tradable security. For more information, <a rel="nofollow" target="_blank" href="https://massive.com/contact">contact us</a>.  |
| `sym` | string | The ticker symbol for the given security. |
| `t` | integer | The nanosecond timestamp. |

## Sample Response

```json
{
  "ev": "FMV",
  "fmv": 33021.9,
  "sym": "X:BTC-USD",
  "t": 1610462007425
}
```
