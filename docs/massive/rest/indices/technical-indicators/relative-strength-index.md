# REST
## Indices

### Relative Strength Index (RSI)

**Endpoint:** `GET /v1/indicators/rsi/{indicesTicker}`

**Description:**

Retrieve the Relative Strength Index (RSI) for a specified ticker over a defined time range. The RSI measures the speed and magnitude of price changes, oscillating between 0 and 100 to help identify overbought or oversold conditions.

Use Cases: Overbought/oversold detection, divergence analysis, trend confirmation, and refining market entry/exit strategies.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `indicesTicker` | string | Yes | The ticker symbol for which to get relative strength index (RSI) data. |
| `timestamp` | string | No | Query by timestamp. Either a date with the format YYYY-MM-DD or a millisecond timestamp. |
| `timespan` | string | No | The size of the aggregate time window. |
| `adjusted` | boolean | No | Whether or not the aggregates used to calculate the relative strength index are adjusted for splits. By default, aggregates are adjusted. Set this to false to get results that are NOT adjusted for splits. |
| `window` | integer | No | The window size used to calculate the relative strength index (RSI). |
| `series_type` | string | No | The value in the aggregate which will be used to calculate the relative strength index. i.e. 'close' will result in using close values to  calculate the relative strength index (RSI). |
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
| `results` | object | The results of the RSI indicator calculation. |
| `results.underlying` | object | The underlying aggregates used. |
| `results.values` | array[object] | Timestamp or indicator value. |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "next_url": "https://api.massive.com/v1/indicators/rsi/I:NDX?cursor=YWRqdXN0ZWQ9dHJ1ZSZhcD0lN0IlMjJ2JTIyJTNBMCUyQyUyMm8lMjIlM0EwJTJDJTIyYyUyMiUzQTE1MDg0Ljk5OTM4OTgyMDAzJTJDJTIyaCUyMiUzQTAlMkMlMjJsJTIyJTNBMCUyQyUyMnQlMjIlM0ExNjg3MjE5MjAwMDAwJTdEJmFzPSZleHBhbmRfdW5kZXJseWluZz1mYWxzZSZsaW1pdD0xJm9yZGVyPWRlc2Mmc2VyaWVzX3R5cGU9Y2xvc2UmdGltZXNwYW49ZGF5JnRpbWVzdGFtcC5sdD0xNjg3MjM3MjAwMDAwJndpbmRvdz0xNA",
  "request_id": "28a8417f521f98e1d08f6ed7d1fbcad3",
  "results": {
    "underlying": {
      "url": "https://api.massive.com/v2/aggs/ticker/I:NDX/range/1/day/1678338000000/1687366658253?limit=66&sort=desc"
    },
    "values": [
      {
        "timestamp": 1687237200000,
        "value": 73.98019439047955
      }
    ]
  },
  "status": "OK"
}
```
