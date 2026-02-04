# REST
## Forex

### Quotes

**Endpoint:** `GET /v3/quotes/{fxTicker}`

**Description:**

Retrieve historical Best Bid and Offer (BBO) quotes for a specified forex currency pair over a defined time range. Each record includes bid/ask prices, exchange identifiers, and timestamps, capturing the prevailing top-of-book prices at each moment. By examining this data, users can analyze currency price movements, assess market liquidity, and refine forex trading or research strategies.

Use Cases: Historical quote analysis, liquidity assessment, algorithmic backtesting, strategy refinement.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `fxTicker` | string | Yes | The ticker symbol to get quotes for. |
| `timestamp` | string | No | Query by timestamp. Either a date with the format YYYY-MM-DD or a nanosecond timestamp. |
| `timestamp.gte` | string | No | Range by timestamp. |
| `timestamp.gt` | string | No | Range by timestamp. |
| `timestamp.lte` | string | No | Range by timestamp. |
| `timestamp.lt` | string | No | Range by timestamp. |
| `order` | string | No | Order results based on the `sort` field. |
| `limit` | integer | No | Limit the number of results returned, default is 1000 and max is 50000. |
| `sort` | string | No | Sort field used for ordering. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page of data. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].ask_exchange` | integer | The ask exchange ID |
| `results[].ask_price` | number | The ask price. |
| `results[].bid_exchange` | integer | The bid exchange ID |
| `results[].bid_price` | number | The bid price. |
| `results[].participant_timestamp` | integer | The nanosecond Exchange Unix Timestamp. This is the timestamp of when the quote was generated at the exchange. |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "next_url": "https://api.massive.com/v3/quotes/C:EUR-USD?cursor=YWN0aXZlPXRydWUmZGF0ZT0yMDIxLTA0LTI1JmxpbWl0PTEmb3JkZXI9YXNjJnBhZ2VfbWFya2VyPUElN0M5YWRjMjY0ZTgyM2E1ZjBiOGUyNDc5YmZiOGE1YmYwNDVkYzU0YjgwMDcyMWE2YmI1ZjBjMjQwMjU4MjFmNGZiJnNvcnQ9dGlja2Vy",
  "request_id": "a47d1beb8c11b6ae897ab76cdbbf35a3",
  "results": [
    {
      "ask_exchange": 48,
      "ask_price": 1.18565,
      "bid_exchange": 48,
      "bid_price": 1.18558,
      "participant_timestamp": 1625097600000000000
    },
    {
      "ask_exchange": 48,
      "ask_price": 1.18565,
      "bid_exchange": 48,
      "bid_price": 1.18559,
      "participant_timestamp": 1625097600000000000
    }
  ],
  "status": "OK"
}
```
