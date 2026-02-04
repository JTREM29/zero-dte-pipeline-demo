# WEBSOCKET
## Stocks

### Net Order Imbalance (NOI)

**Endpoint:** `WS /stocks/NOI`

**Description:**

Stream real-time Net Order Imbalance (NOI) events for specified stock tickers via WebSocket. Focused on NYSE listed securities, this feed delivers updates on buy and sell order imbalances during scheduled exchange auctions. Typically, at market open (9:30 AM ET) and close (4:00 PM ET), along with indicative clearing prices, paired quantities, and imbalance amounts. Intraday events may also occur during ticker specific halts or mini-auctions, offering insights into liquidity pressures and auction dynamics.

Use Cases: Auction price discovery, execution timing, liquidity monitoring, short-term trading signals, market event analysis.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | Specify a stock ticker or use * to subscribe to all stock tickers. You can also use a comma separated list to subscribe to multiple stock tickers. You can retrieve available stock tickers from our [Stock Tickers API](https://massive.com/docs/rest/stocks/tickers/all-tickers).  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ev` | enum: NOI | The event type. |
| `T` | string | The ticker symbol for the given stock. |
| `t` | integer | The Timestamp in Unix MS. |
| `at` | integer | The time that the auction is planned to take place in the format (hour x 100) + minutes in Eastern Standard Time,  for example 930 would be 9:30 am EST, and 1600 would be 4:00 pm EST.  |
| `a` | string | The auction type. `O` - Early Opening Auction (non-NYSE only) `M` - Core Opening Auction `H` - Reopening Auction (Halt Resume) `C` - Closing Auction `P` - Extreme Closing Imbalance (NYSE only) `R` - Regulatory Closing Imbalance (NYSE only)  |
| `i` | integer | The symbol sequence. |
| `x` | integer | The exchange ID. See <a target="_blank" href="https://massive.com/docs/rest/stocks/market-operations/exchanges" alt="Exchanges">Exchanges</a> for Massive's mapping of exchange IDs. |
| `o` | integer | The imbalance quantity. |
| `p` | integer | The paired quantity. |
| `b` | number | The book clearing price. |

## Sample Response

```json
{
  "ev": "NOI",
  "T": "NTEST.Q",
  "t": 1601318039223013600,
  "at": 930,
  "a": "M",
  "i": 44,
  "x": 10,
  "o": 480,
  "p": 440,
  "b": 25.03
}
```
