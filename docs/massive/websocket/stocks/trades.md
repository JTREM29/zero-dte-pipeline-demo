# WEBSOCKET
## Stocks

### Trades

**Endpoint:** `WS /stocks/T`

**Description:**

Stream tick-level trade data for stock tickers via WebSocket. Each message delivers key trade details (price, size, exchange, conditions, and timestamps) as they occur, enabling users to track market activity, power live dashboards, and inform rapid decision-making.

Use Cases: Live monitoring, algorithmic trading, market analysis, data visualization.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a stock ticker or use * to subscribe to all stock tickers. You can also use a comma separated list to subscribe to multiple stock tickers. You can retrieve available stock tickers from our [Stock Tickers API](https://massive.com/docs/rest/stocks/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: T | The event type. |
| `sym` | string | The ticker symbol for the given stock. |
| `x` | integer | The exchange ID. See <a target="_blank" href="https://massive.com/docs/rest/stocks/market-operations/exchanges" alt="Exchanges">Exchanges</a> for Massive's mapping of exchange IDs. |
| `i` | string | The trade ID. |
| `z` | integer | The tape. (1 = NYSE, 2 = AMEX, 3 = Nasdaq).  |
| `p` | number | The price. |
| `s` | integer | The trade size. |
| `c` | array[integer] | The trade conditions. See <a target="_blank" href="https://massive.com/glossary/us/stocks/conditions-indicators"  alt="Conditions and Indicators">Conditions and Indicators</a> for Massive's trade conditions glossary.  |
| `t` | integer | The SIP timestamp in Unix MS. |
| `q` | integer | The sequence number represents the sequence in which message events happened. These are increasing and unique per ticker symbol, but will not always be sequential (e.g., 1, 2, 6, 9, 10, 11).  |
| `trfi` | integer | The ID for the Trade Reporting Facility where the trade took place. |
| `trft` | integer | The TRF (Trade Reporting Facility) Timestamp in Unix MS.  This is the timestamp of when the trade reporting facility received this trade.  |

## Sample Response

```json
{
  "ev": "T",
  "sym": "MSFT",
  "x": 4,
  "i": "12345",
  "z": 3,
  "p": 114.125,
  "s": 100,
  "c": [
    0,
    12
  ],
  "t": 1536036818784,
  "q": 3681328
}
```
