# REST
## Indices

### Moving Average Convergence/Divergence (MACD)

**Endpoint:** `GET /v1/indicators/macd/{indicesTicker}`

**Description:**

Retrieve the Moving Average Convergence/Divergence (MACD) for a specified ticker over a defined time range. MACD is a momentum indicator derived from two moving averages, helping to identify trend strength, direction, and potential trading signals.

Use Cases: Momentum analysis, signal generation (crossover events), spotting overbought/oversold conditions, and confirming trend directions.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `indicesTicker` | string | Yes | The ticker symbol for which to get MACD data. |
| `timestamp` | string | No | Query by timestamp. Either a date with the format YYYY-MM-DD or a millisecond timestamp. |
| `timespan` | string | No | The size of the aggregate time window. |
| `adjusted` | boolean | No | Whether or not the aggregates used to calculate the MACD are adjusted for splits. By default, aggregates are adjusted. Set this to false to get results that are NOT adjusted for splits. |
| `short_window` | integer | No | The short window size used to calculate MACD data. |
| `long_window` | integer | No | The long window size used to calculate MACD data. |
| `signal_window` | integer | No | The window size used to calculate the MACD signal line. |
| `series_type` | string | No | The value in the aggregate which will be used to calculate the MACD. i.e. 'close' will result in using close values to  calculate the MACD. |
| `expand_underlying` | boolean | No | Whether or not to include the aggregates used to calculate this indicator in the response. |
| `order` | string | No | The order in which to return the results, ordered by timestamp. |
| `limit` | integer | No | Limit the number of results returned, default is 10 and max is 5000 |
| `timestamp.gte` | string | No | Range by timestamp. |
| `timestamp.gt` | string | No | Range by timestamp. |
| `timestamp.lte` | string | No | Range by timestamp. |
| `timestamp.lt` | string | No | Range by timestamp. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page of data. |
| `request_id` | string | A request id assigned by the server. |
| `results` | object | The results of the MACD indicator calculation. |
| `results.underlying` | object | The underlying aggregates used. |
| `results.values` | array[object] | Each entry in the values array represents MACD indicator data for a specific timestamp and includes: |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "next_url": "https://api.massive.com/v1/indicators/macd/I:NDX?cursor=YWRqdXN0ZWQ9dHJ1ZSZhcD0lN0IlMjJ2JTIyJTNBMCUyQyUyMm8lMjIlM0EwJTJDJTIyYyUyMiUzQTE1MDg0Ljk5OTM4OTgyMDAzJTJDJTIyaCUyMiUzQTAlMkMlMjJsJTIyJTNBMCUyQyUyMnQlMjIlM0ExNjg3MTUwODAwMDAwJTdEJmFzPSZleHBhbmRfdW5kZXJseWluZz1mYWxzZSZsaW1pdD0yJmxvbmdfd2luZG93PTI2Jm9yZGVyPWRlc2Mmc2VyaWVzX3R5cGU9Y2xvc2Umc2hvcnRfd2luZG93PTEyJnNpZ25hbF93aW5kb3c9OSZ0aW1lc3Bhbj1kYXkmdGltZXN0YW1wLmx0PTE2ODcyMTkyMDAwMDA",
  "request_id": "2eeda0be57e83cbc64cc8d1a74e84dbd",
  "results": {
    "underlying": {
      "url": "https://api.massive.com/v2/aggs/ticker/I:NDX/range/1/day/1678338000000/1687366537196?limit=121&sort=desc"
    },
    "values": [
      {
        "histogram": -4.7646219788108795,
        "signal": 220.7596784587801,
        "timestamp": 1687237200000,
        "value": 215.9950564799692
      },
      {
        "histogram": 3.4518937661882205,
        "signal": 221.9508339534828,
        "timestamp": 1687219200000,
        "value": 225.40272771967102
      }
    ]
  },
  "status": "OK"
}
```
