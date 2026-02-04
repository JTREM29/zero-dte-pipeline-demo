# REST
## Crypto

### Previous Day Bar (OHLC)

**Endpoint:** `GET /v2/aggs/ticker/{cryptoTicker}/prev`

**Description:**

Retrieve the previous trading day's open, high, low, and close (OHLC) data for a specified cryptocurrency pair. This endpoint provides key pricing metrics, including volume, to help users assess recent performance and inform trading strategies.

Use Cases: Baseline comparison, technical analysis, market research, and daily reporting.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `cryptoTicker` | string | Yes | The ticker symbol of the currency pair. |
| `adjusted` | boolean | No | Whether or not the results are adjusted for splits.  By default, results are adjusted. Set this to false to get results that are NOT adjusted for splits.  |

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
  "queryCount": 1,
  "request_id": "b2170df985474b6d21a6eeccfb6bee67",
  "results": [
    {
      "T": "X:BTCUSD",
      "c": 16035.9,
      "h": 16180,
      "l": 15639.2,
      "o": 15937.1,
      "t": 1605416400000,
      "v": 95045.16897951,
      "vw": 15954.2111
    }
  ],
  "resultsCount": 1,
  "status": "OK",
  "ticker": "X:BTCUSD"
}
```
