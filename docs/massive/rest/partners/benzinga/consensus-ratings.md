# REST
## Partners

### Consensus Ratings

**Endpoint:** `GET /benzinga/v1/consensus-ratings/{ticker}`

**Description:**

Retrieve consensus ratings from financial analysts, including aggregated rating distributions and price target ranges. Each record reflects the collective outlook for a security, summarizing sentiment across firms and analysts. This dataset supports trend analysis and offers a high-level view of market expectations.

Use Cases: Sentiment aggregation, investment research, peer comparison.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `date` | string | No | The date range to aggregate analyst ratings over. For example, date.gte=2024-10-01 and date.lt=2025-01-01 for ratings published in Q4 2024. By default, all ratings are aggregated regardless of date. |
| `date.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `date.gt` | string | No | Filter greater than the value. |
| `date.gte` | string | No | Filter greater than or equal to the value. |
| `date.lt` | string | No | Filter less than the value. |
| `date.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `ticker` | string | Yes | The requested ticker. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].buy_ratings` | integer | The count of 'Buy' ratings from contributing analysts. |
| `results[].consensus_price_target` | number | The average price target across all analysts, rounded to 2 decimal places. |
| `results[].consensus_rating` | string | The overall rating category determined by the average consensus weight. Possible values: 'strong_buy', 'buy', 'hold', 'sell', 'strong_sell'. |
| `results[].consensus_rating_value` | number | The numerical average of all consensus weights, rounded to 2 decimal places. Scale ranges from 1 (Strong Sell) to 5 (Strong Buy). |
| `results[].high_price_target` | number | The highest price target among all contributing analysts. |
| `results[].hold_ratings` | integer | The count of 'Hold' ratings from contributing analysts. |
| `results[].low_price_target` | number | The lowest price target among all contributing analysts. |
| `results[].price_target_contributors` | integer | The number of unique analysts contributing price targets. |
| `results[].ratings_contributors` | integer | The number of unique analysts contributing to the overall ratings consensus. |
| `results[].sell_ratings` | integer | The count of 'Sell' ratings from contributing analysts. |
| `results[].strong_buy_ratings` | integer | The count of 'Strong Buy' ratings from contributing analysts. |
| `results[].strong_sell_ratings` | integer | The count of 'Strong Sell' ratings from contributing analysts. |
| `results[].ticker` | string | The requested ticker. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "buy_ratings": 6,
      "consensus_price_target": 23.28,
      "consensus_rating": "hold",
      "consensus_rating_value": 4.14,
      "high_price_target": 32.14,
      "hold_ratings": 3,
      "low_price_target": 6.34,
      "price_target_contributors": 15,
      "ratings_contributors": 14,
      "sell_ratings": 0,
      "strong_buy_ratings": 5,
      "strong_sell_ratings": 0,
      "ticker": "AAPL"
    }
  ],
  "status": "OK"
}
```
