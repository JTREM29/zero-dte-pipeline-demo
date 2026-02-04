# REST
## Crypto

### Custom Bars (OHLC)

**Endpoint:** `GET /v2/aggs/ticker/{cryptoTicker}/range/{multiplier}/{timespan}/{from}/{to}`

**Description:**

Retrieve aggregated historical OHLC (Open, High, Low, Close) and volume data for a specified cryptocurrency pair over a custom date range and time interval in Coordinated Universal Time (UTC). These aggregates are derived from qualifying crypto trades that meet specific conditions. If no eligible trades occur within a given timeframe, no aggregate bar is generated, resulting in an empty interval that transparently indicates a period without trading activity. Users can adjust the multiplier and timespan parameters (e.g., a 5-minute bar) to tailor their analysis. This flexibility supports a wide range of analytical and visualization needs within the crypto markets.

Use Cases: Data visualization, technical analysis, backtesting strategies, market research.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `cryptoTicker` | string | Yes | The ticker symbol of the currency pair. |
| `multiplier` | integer | Yes | The size of the timespan multiplier. |
| `timespan` | string | Yes | The size of the time window. |
| `from` | string | Yes | The start of the aggregate time window. Either a date with the format YYYY-MM-DD or a millisecond timestamp. |
| `to` | string | Yes | The end of the aggregate time window. Either a date with the format YYYY-MM-DD or a millisecond timestamp. |
| `adjusted` | boolean | No | Whether or not the results are adjusted for splits.  By default, results are adjusted. Set this to false to get results that are NOT adjusted for splits.  |
| `sort` | N/A | No | Sort the results by timestamp. `asc` will return results in ascending order (oldest at the top), `desc` will return results in descending order (newest at the top).  |
| `limit` | integer | No | Limits the number of base aggregates queried to create the aggregate results. Max 50000 and Default 5000. Read more about how limit is used to calculate aggregate results in our article on <a href="https://massive.com/blog/aggs-api-updates/" target="_blank" alt="Aggregate Data API Improvements">Aggregate Data API Improvements</a>.  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ticker` | string | The exchange symbol that this item is traded under. |
| `adjusted` | boolean | Whether or not this response was adjusted for splits. |
| `queryCount` | integer | The number of aggregates (minute or day) used to generate the response. |
| `request_id` | string | A request id assigned by the server. |
| `resultsCount` | integer | The total number of results for this request. |
| `status` | string | The status of this request's response. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].c` | number | The close price for the symbol in the given time period. |
| `results[].h` | number | The highest price for the symbol in the given time period. |
| `results[].l` | number | The lowest price for the symbol in the given time period. |
| `results[].n` | integer | The number of transactions in the aggregate window. |
| `results[].o` | number | The open price for the symbol in the given time period. |
| `results[].t` | integer | The Unix millisecond timestamp for the start of the aggregate window. |
| `results[].v` | number | The trading volume of the symbol in the given time period. |
| `results[].vw` | number | The volume weighted average price. |

## Sample Response

```json
{
  "adjusted": true,
  "queryCount": 2,
  "request_id": "0cf72b6da685bcd386548ffe2895904a",
  "results": [
    {
      "c": 10094.75,
      "h": 10429.26,
      "l": 9490,
      "n": 1,
      "o": 9557.9,
      "t": 1590984000000,
      "v": 303067.6562332156,
      "vw": 9874.5529
    },
    {
      "c": 9492.62,
      "h": 10222.72,
      "l": 9135.68,
      "n": 1,
      "o": 10096.87,
      "t": 1591070400000,
      "v": 323339.6922892879,
      "vw": 9729.5701
    }
  ],
  "resultsCount": 2,
  "status": "OK",
  "ticker": "X:BTCUSD"
}
```
