# REST
## Options

### Custom Bars (OHLC)

**Endpoint:** `GET /v2/aggs/ticker/{optionsTicker}/range/{multiplier}/{timespan}/{from}/{to}`

**Description:**

Retrieve aggregated historical OHLC (Open, High, Low, Close) and volume data for a specified options contract over a custom date range and time interval in Eastern Time (ET). Aggregates are derived exclusively from qualifying trades that meet specific conditions. If no eligible trades occur within a given timeframe, no aggregate bar is produced, resulting in an empty interval that indicates a lack of trading activity during that period. Users can tailor their data by adjusting the multiplier and timespan parameters (e.g., a 5-minute bar). This flexibility supports a broad range of analytical and visualization needs for options markets.

Use Cases: Data visualization, technical analysis, backtesting strategies, market research.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `optionsTicker` | string | Yes | The ticker symbol of the options contract. |
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
  "count": 2,
  "queryCount": 2,
  "request_id": "5585acde-5085-42d6-95b2-2e388a28370a",
  "results": [
    {
      "c": 26.2,
      "h": 26.2,
      "l": 26.2,
      "n": 1,
      "o": 26.2,
      "t": 1632369600000,
      "v": 2,
      "vw": 26.2
    },
    {
      "c": 28.3,
      "h": 28.3,
      "l": 28.3,
      "n": 1,
      "o": 28.3,
      "t": 1632456000000,
      "v": 2,
      "vw": 28.3
    }
  ],
  "resultsCount": 2,
  "status": "OK",
  "ticker": "O:RDFN211119C00025000"
}
```
