# REST
## Forex

### Daily Market Summary (OHLC)

**Endpoint:** `GET /v2/aggs/grouped/locale/global/market/fx/{date}`

**Description:**

Retrieve daily OHLC (open, high, low, close), volume, and volume-weighted average price (VWAP) data for all forex tickers on a specified trading date. This endpoint returns comprehensive market coverage in a single request, enabling wide-scale analysis, bulk data processing, and research into broad market performance.

Use Cases: Market overview, bulk data processing, historical research, and portfolio comparison.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `date` | string | Yes | The beginning date for the aggregate window. |
| `adjusted` | boolean | No | Whether or not the results are adjusted for splits.  By default, results are adjusted. Set this to false to get results that are NOT adjusted for splits.  |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `adjusted` | boolean | Whether or not this response was adjusted for splits. |
| `queryCount` | integer | The number of aggregates (minute or day) used to generate the response. |
| `request_id` | string | A request id assigned by the server. |
| `resultsCount` | integer | The total number of results for this request. |
| `status` | string | The status of this request's response. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].T` | string | The exchange symbol that this item is traded under. |
| `results[].c` | number | The close price for the symbol in the given time period. |
| `results[].h` | number | The highest price for the symbol in the given time period. |
| `results[].l` | number | The lowest price for the symbol in the given time period. |
| `results[].n` | integer | The number of transactions in the aggregate window. |
| `results[].o` | number | The open price for the symbol in the given time period. |
| `results[].t` | integer | The Unix millisecond timestamp for the end of the aggregate window. |
| `results[].v` | number | The trading volume of the symbol in the given time period. |
| `results[].vw` | number | The volume weighted average price. |

## Sample Response

```json
{
  "adjusted": true,
  "queryCount": 3,
  "request_id": {
    "description": "A request id assigned by the server.",
    "type": "string"
  },
  "results": [
    {
      "T": "C:ILSCHF",
      "c": 0.2704,
      "h": 0.2706,
      "l": 0.2693,
      "n": 689,
      "o": 0.2698,
      "t": 1602719999999,
      "v": 689,
      "vw": 0.2702
    },
    {
      "T": "C:GBPCAD",
      "c": 1.71103,
      "h": 1.71642,
      "l": 1.69064,
      "n": 407324,
      "o": 1.69955,
      "t": 1602719999999,
      "v": 407324,
      "vw": 1.7062
    },
    {
      "T": "C:DKKAUD",
      "c": 0.2214,
      "h": 0.2214,
      "l": 0.2195,
      "n": 10639,
      "o": 0.22,
      "t": 1602719999999,
      "v": 10639,
      "vw": 0.2202
    }
  ],
  "resultsCount": 3,
  "status": "OK"
}
```
