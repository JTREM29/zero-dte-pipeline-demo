# REST
## Futures

### Trades

**Endpoint:** `GET /futures/vX/trades/{ticker}`

**Description:**

Retrieve comprehensive, tick-level trade data for a specified futures contract ticker over a defined time range. Each record includes the trade price, size, session start date, and precise timestamps, capturing individual trade events throughout the period. This granular data is essential for constructing aggregated bars and performing detailed analyses of intraday price movements, making it a valuable tool for backtesting, algorithmic strategy development, and market research.

Use Cases: Intraday analysis, algorithmic trading, backtesting, market research.

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
| `results[].price` | number | The price of the trade. This is the actual dollar value per whole contract of this trade. A trade of 100 contracts with a price of $2.00 would be worth a total dollar value of $200.00. |
| `results[].session_end_date` | string | Also known as the trading date, the date of the end of the trading session, in YYYY-MM-DD format. |
| `results[].size` | number | The total number of contracts exchanged between buyers and sellers on a given trade. |
| `results[].ticker` | string | ticker of the trade |
| `results[].timestamp` | integer | The time when the trade was generated at the exchange to nanosecond precision. |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "request_id": "a47d1beb8c11b6ae897ab76cdbbf35a3",
  "results": [
    {
      "price": 605400,
      "session_end_date": "2024-12-17",
      "size": 12,
      "ticker": "ESZ4",
      "timestamp": 1734484219379895800
    },
    {
      "price": 605412,
      "session_end_date": "2024-12-17",
      "size": 55,
      "ticker": "ESZ4",
      "timestamp": 1734484212100118500
    },
    {
      "price": 605544,
      "session_end_date": "2024-12-17",
      "size": 36,
      "ticker": "ESZ4",
      "timestamp": 1734484212099944200
    }
  ],
  "status": "OK"
}
```
