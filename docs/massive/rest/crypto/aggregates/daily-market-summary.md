# REST
## Crypto

### Daily Market Summary (OHLC)

**Endpoint:** `GET /v2/aggs/grouped/locale/global/market/crypto/{date}`

**Description:**

Retrieve daily OHLC (open, high, low, close), volume, and volume-weighted average price (VWAP) data for all crypto tickers on a specified trading date. This endpoint returns comprehensive market coverage in a single request, enabling wide-scale analysis, bulk data processing, and research into broad market performance.

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
      "T": "X:ARDRUSD",
      "c": 0.0550762,
      "h": 0.0550762,
      "l": 0.0550762,
      "n": 18388,
      "o": 0.0550762,
      "t": 1580676480000,
      "v": 2,
      "vw": 0.0551
    },
    {
      "T": "X:NGCUSD",
      "c": 0.0272983,
      "h": 0.0273733,
      "l": 0.0272983,
      "n": 18,
      "o": 0.0273733,
      "t": 1580674080000,
      "v": 4734,
      "vw": 0.0273
    },
    {
      "T": "X:ZSCUSD",
      "c": 0.00028531,
      "h": 0.00028531,
      "l": 0.00028531,
      "n": 151,
      "o": 0.00028531,
      "t": 1580671080000,
      "v": 390,
      "vw": 0.0003
    }
  ],
  "resultsCount": 3,
  "status": "OK"
}
```
