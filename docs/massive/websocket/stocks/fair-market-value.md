# WEBSOCKET
## Stocks

### Fair Market Value

**Endpoint:** `WS /business/stocks/FMV`

**Description:**

Stream real-time Fair Market Value (FMV) data for a specified stock ticker via WebSocket. This proprietary metric, available exclusively to Business plan users, provides an algorithmically derived, real-time estimate of a security’s fair market price. By delivering accurate, continuous valuation data, this feed supports informed trading decisions, enhanced analytics, and more effective risk management.

Use Cases: Pricing strategies, algorithmic modeling, risk assessment, investor decision-making.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a stock ticker or use * to subscribe to all stock tickers. You can also use a comma separated list to subscribe to multiple stock tickers. You can retrieve available stock tickers from our [Stock Tickers API](https://massive.com/docs/rest/stocks/tickers/all-tickers).  |

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
  "fmv": 189.22,
  "sym": "AAPL",
  "t": 1678220098130
}
```
