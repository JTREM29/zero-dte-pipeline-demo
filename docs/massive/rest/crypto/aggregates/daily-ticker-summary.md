# REST
## Crypto

### Daily Ticker Summary (OHLC)

**Endpoint:** `GET /v1/open-close/crypto/{from}/{to}/{date}`

**Description:**

Retrieve the opening and closing trades for a specific crypto pair on a given date. This endpoint provides essential daily pricing details, enabling users to evaluate performance, conduct historical analysis, and gain insights into trading activity.

Use Cases: Daily performance analysis, historical data collection, portfolio tracking.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `from` | string | Yes | The "from" symbol of the pair. |
| `to` | string | Yes | The "to" symbol of the pair. |
| `date` | string | Yes | The date of the requested open/close in the format YYYY-MM-DD. |
| `adjusted` | boolean | No | Whether or not the results are adjusted for splits.  By default, results are adjusted. Set this to false to get results that are NOT adjusted for splits.  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `close` | number | The close price for the symbol in the given time period. |
| `closingTrades` | array[object] | An array of results containing the requested data. |
| `closingTrades[].c` | array[integer] | A list of condition codes.  |
| `closingTrades[].i` | string | The Trade ID which uniquely identifies a trade. These are unique per combination of ticker, exchange, and TRF. For example: A trade for AAPL executed on NYSE and a trade for AAPL executed on NASDAQ could potentially have the same Trade ID.  |
| `closingTrades[].p` | number | The price of the trade. This is the actual dollar value per whole share of this trade. A trade of 100 shares with a price of $2.00 would be worth a total dollar value of $200.00.  |
| `closingTrades[].s` | number | The size of a trade (also known as volume).  |
| `closingTrades[].t` | integer | The Unix millisecond timestamp for the start of the aggregate window. |
| `closingTrades[].x` | integer | The exchange that this crypto trade happened on.   See <a href="https://massive.com/docs/rest/crypto/market-operations/exchanges">Exchanges</a> for a mapping of exchanges to IDs.  |
| `day` | string | The date requested. |
| `isUTC` | boolean | Whether or not the timestamps are in UTC timezone. |
| `open` | number | The open price for the symbol in the given time period. |
| `openTrades` | array[object] | An array of results containing the requested data. |
| `openTrades[].c` | array[integer] | A list of condition codes.  |
| `openTrades[].i` | string | The Trade ID which uniquely identifies a trade. These are unique per combination of ticker, exchange, and TRF. For example: A trade for AAPL executed on NYSE and a trade for AAPL executed on NASDAQ could potentially have the same Trade ID.  |
| `openTrades[].p` | number | The price of the trade. This is the actual dollar value per whole share of this trade. A trade of 100 shares with a price of $2.00 would be worth a total dollar value of $200.00.  |
| `openTrades[].s` | number | The size of a trade (also known as volume).  |
| `openTrades[].t` | integer | The Unix millisecond timestamp for the start of the aggregate window. |
| `openTrades[].x` | integer | The exchange that this crypto trade happened on.   See <a href="https://massive.com/docs/rest/crypto/market-operations/exchanges">Exchanges</a> for a mapping of exchanges to IDs.  |
| `symbol` | string | The symbol pair that was evaluated from the request. |

## Sample Response

```json
{
  "close": 11050.64,
  "closingTrades": [
    {
      "c": [
        2
      ],
      "i": "973323250",
      "p": 11050.64,
      "s": 0.006128,
      "t": 1602287999795,
      "x": 4
    },
    {
      "c": [
        1
      ],
      "i": "105717893",
      "p": 11049.4,
      "s": 0.014,
      "t": 1602287999659,
      "x": 17
    }
  ],
  "day": "2020-10-09T00:00:00.000Z",
  "isUTC": true,
  "open": 10932.44,
  "openTrades": [
    {
      "c": [
        2
      ],
      "i": "511235746",
      "p": 10932.44,
      "s": 0.002,
      "t": 1602201600056,
      "x": 1
    },
    {
      "c": [
        2
      ],
      "i": "511235751",
      "p": 10923.76,
      "s": 0.02,
      "t": 1602201600141,
      "x": 4
    }
  ],
  "symbol": "BTC-USD"
}
```
