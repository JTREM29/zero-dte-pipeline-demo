# WEBSOCKET
## Stocks

### Quotes

**Endpoint:** `WS /stocks/Q`

**Description:**

Stream NBBO (National Best Bid and Offer) quote data for stock tickers via WebSocket. Each message provides the current best bid/ask prices, sizes, and related metadata as they update, allowing users to monitor evolving market conditions, inform trading decisions, and maintain responsive, data-driven applications.

Use Cases: Live monitoring, market analysis, trading decision support, dynamic interface updates.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a stock ticker or use * to subscribe to all stock tickers. You can also use a comma separated list to subscribe to multiple stock tickers. You can retrieve available stock tickers from our [Stock Tickers API](https://massive.com/docs/rest/stocks/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: Q | The event type. |
| `sym` | string | The ticker symbol for the given stock. |
| `bx` | integer | The bid exchange ID. |
| `bp` | number | The bid price. |
| `bs` | integer | The bid size. This represents the number of round lot orders at the given bid price. The normal round lot size is 100 shares. A bid size of 2 means there are 200 shares for purchase at the given bid price. |
| `ax` | integer | The ask exchange ID. |
| `ap` | number | The ask price. |
| `as` | integer | The ask size. This represents the number of round lot orders at the given ask price. The normal round lot size is 100 shares. An ask size of 2 means there are 200 shares available to purchase at the given ask price. |
| `c` | integer | The condition. |
| `i` | array[integer] | The indicators. For more information, see our glossary of [Conditions and Indicators](https://massive.com/glossary/us/stocks/conditions-indicators).  |
| `t` | integer | The SIP timestamp in Unix MS. |
| `q` | integer | The sequence number represents the sequence in which quote events happened. These are increasing and unique per ticker symbol, but will not always be sequential (e.g., 1, 2, 6, 9, 10, 11). Values reset after each trading session/day.  |
| `z` | integer | The tape. (1 = NYSE, 2 = AMEX, 3 = Nasdaq). |

## Sample Response

```json
{
  "ev": "Q",
  "sym": "MSFT",
  "bx": 4,
  "bp": 114.125,
  "bs": 100,
  "ax": 7,
  "ap": 114.128,
  "as": 160,
  "c": 0,
  "i": [
    604
  ],
  "t": 1536036818784,
  "q": 50385480,
  "z": 3
}
```
