# REST
## Indices

### Custom Bars (OHLC)

**Endpoint:** `GET /v2/aggs/ticker/{indicesTicker}/range/{multiplier}/{timespan}/{from}/{to}`

**Description:**

Retrieve aggregated historical OHLC (Open, High, Low, Close) and value data for a specified index over a custom date range and time interval in Eastern Time (ET). Unlike stocks or options, these aggregates are derived from index values rather than individual trades, reflecting the performance of a market segment, sector, or benchmark. If no index updates occur within a given timeframe, no aggregate bar is produced, resulting in an empty interval that indicates a period without new index data. Users can customize their view by adjusting the multiplier and timespan parameters (e.g., a 5-minute interval). This approach supports various analytical and visualization needs related to broad market or sector performance.

Use Cases: Data visualization, market trend analysis, benchmark comparisons, research and modeling.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `indicesTicker` | string | Yes | The ticker symbol of Index. |
| `multiplier` | integer | Yes | The size of the timespan multiplier. |
| `timespan` | string | Yes | The size of the time window. |
| `from` | string | Yes | The start of the aggregate time window. Either a date with the format YYYY-MM-DD or a millisecond timestamp. |
| `to` | string | Yes | The end of the aggregate time window. Either a date with the format YYYY-MM-DD or a millisecond timestamp. |
| `sort` | N/A | No | Sort the results by timestamp. `asc` will return results in ascending order (oldest at the top), `desc` will return results in descending order (newest at the top).  |
| `limit` | integer | No | Limits the number of base aggregates queried to create the aggregate results. Max 50000 and Default 5000. Read more about how limit is used to calculate aggregate results in our article on <a href="https://massive.com/blog/aggs-api-updates/" target="_blank" alt="Aggregate Data API Improvements">Aggregate Data API Improvements</a>.  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `ticker` | string | The exchange symbol that this item is traded under. |
| `queryCount` | integer | The number of aggregates (minute or day) used to generate the response. |
| `request_id` | string | A request id assigned by the server. |
| `resultsCount` | integer | The total number of results for this request. |
| `status` | string | The status of this request's response. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].c` | number | The close value for the symbol in the given time period. |
| `results[].h` | number | The highest value for the symbol in the given time period. |
| `results[].l` | number | The lowest value for the symbol in the given time period. |
| `results[].o` | number | The open value for the symbol in the given time period. |
| `results[].t` | integer | The Unix millisecond timestamp for the start of the aggregate window. |

## Sample Response

```json
{
  "count": 2,
  "queryCount": 2,
  "request_id": "0cf72b6da685bcd386548ffe2895904a",
  "results": [
    {
      "c": 11995.88235998666,
      "h": 12340.44936267155,
      "l": 11970.34221717375,
      "o": 12230.83658266843,
      "t": 1678341600000
    },
    {
      "c": 11830.28178808306,
      "h": 12069.62262033557,
      "l": 11789.85923449393,
      "o": 12001.69552583921,
      "t": 1678428000000
    }
  ],
  "status": "OK",
  "ticker": "I:NDX"
}
```
