# REST
## Futures

### Quotes

**Endpoint:** `GET /futures/vX/quotes/{ticker}`

**Description:**

Retrieve quote data for a specified futures contract ticker. Each record includes the best bid and offer prices, sizes, and timestamps, reflecting the prevailing quote environment at each moment. This endpoint supports detailed analysis of price dynamics and liquidity conditions to inform trading decisions and market research.

Use Cases: Liquidity analysis, price discovery, trading strategy refinement, market research.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | The futures contract identifier, including the base symbol and contract expiration (e.g., GCJ5 for the April 2025 gold contract). |
| `timestamp` | string | No | Query by trade timestamp. Either a date with the format YYYY-MM-DD or a nanosecond timestamp. |
| `session_end_date` | string | No | Also known as the trading date, the date of the end of the trading session, in YYYY-MM-DD format. |
| `limit` | integer | No | The number of results to return per page (default=1000, maximum=50000, minimum=1). |
| `timestamp.gte` | string | No | Range by timestamp. |
| `timestamp.gt` | string | No | Range by timestamp. |
| `timestamp.lte` | string | No | Range by timestamp. |
| `timestamp.lt` | string | No | Range by timestamp. |
| `session_end_date.gte` | string | No | Range by session_end_date. |
| `session_end_date.gt` | string | No | Range by session_end_date. |
| `session_end_date.lte` | string | No | Range by session_end_date. |
| `session_end_date.lt` | string | No | Range by session_end_date. |
| `sort` | string | No | Sort results by field and direction using dotted notation (e.g., 'ticker.asc', 'name.desc'). |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page of data. |
| `results` | array[object] | N/A |
| `results[].ask_price` | number | The ask price is expressed per unit of the underlying asset, and you apply the contract multiplier to get the full contract value. |
| `results[].ask_size` | number | The quote size represents the number of futures contracts available at the given ask price. |
| `results[].ask_timestamp` | integer | The time when the ask price was submitted to the exchange. |
| `results[].bid_price` | number | The bid price is expressed per unit of the underlying asset, and you apply the contract multiplier to get the full contract value. |
| `results[].bid_size` | number | The quote size represents the number of futures contracts available at the given bid price. |
| `results[].bid_timestamp` | integer | The time when the bid price was submitted to the exchange. |
| `results[].session_end_date` | string | Also known as the trading date, the date of the end of the trading session, in YYYY-MM-DD format. |
| `results[].ticker` | string | The futures contract identifier, including the base symbol and contract expiration (e.g., GCJ5 for the April 2025 gold contract). |
| `results[].timestamp` | integer | The time when the quote was generated at the exchange to nanosecond precision. |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "request_id": "a47d1beb8c11b6ae897ab76cdbbf35a3",
  "results": [
    {
      "ask_price": 604075,
      "ask_size": 6,
      "ask_timestamp": 1734467086235923000,
      "bid_price": 604075,
      "bid_size": 6,
      "bid_timestamp": 1734415473057885400,
      "session_end_date": "2024-12-17",
      "ticker": "ESZ4",
      "timestamp": 1734476400002853000
    },
    {
      "ask_price": 604100,
      "ask_size": 2,
      "ask_timestamp": 1734466901129138400,
      "bid_price": 604100,
      "bid_size": 2,
      "bid_timestamp": 1734415473057885400,
      "session_end_date": "2024-12-17",
      "ticker": "ESZ4",
      "timestamp": 1734466901128680000
    }
  ],
  "status": "OK"
}
```
