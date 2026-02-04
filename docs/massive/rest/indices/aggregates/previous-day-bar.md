# REST
## Indices

### Previous Day Bar (OHLC)

**Endpoint:** `GET /v2/aggs/ticker/{indicesTicker}/prev`

**Description:**

Retrieve the previous trading day's open, high, low, and close (OHLC) data for a specified index ticker. This endpoint provides key pricing metrics, including volume, to help users assess recent performance and inform trading strategies.

Use Cases: Baseline comparison, technical analysis, market research, and daily reporting.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `indicesTicker` | string | Yes | The ticker symbol of Index. |

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
  "queryCount": 1,
  "request_id": "b2170df985474b6d21a6eeccfb6bee67",
  "results": [
    {
      "T": "I:NDX",
      "c": 15070.14948566977,
      "h": 15127.4195807999,
      "l": 14946.7243781848,
      "o": 15036.48391066877,
      "t": 1687291200000
    }
  ],
  "resultsCount": 1,
  "status": "OK",
  "ticker": "I:NDX"
}
```
