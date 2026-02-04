# REST
## Futures

### Aggregate Bars (OHLC)

**Endpoint:** `GET /futures/vX/aggs/{ticker}`

**Description:**

Retrieve aggregated historical OHLC (Open, High, Low, Close) and volume data for a specified futures contract ticker over a custom date range and time interval in Central Time (CT). Aggregates are constructed from all trades during the period. If no trades occur within a given timeframe, no aggregate bar is produced, indicating a period of inactivity. Users can tailor their data by adjusting the multiplier and timespan parameters (e.g., a 5-minute bar), supporting a wide range of analytical and visualization needs.

Use Cases: Market monitoring, technical analysis, backtesting, trading strategy development.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | Yes | The futures contract identifier, including the base symbol and contract expiration (e.g., GCJ5 for the April 2025 gold contract). |
| `resolution` | string | No | This sets the size of the aggregate windows. It accepts custom values that specify the granularity and the duration of the window. For example: 15mins, 30secs, 12hours, or 7days. There are maximum allowable candle sizes. For example, you can request "1min" to "59mins", but after that you will need to use "1hr". If you make a request for a candle size that is not supported, we will return a 400 "Bad Request - resolution value is not allowed." |
| `window_start` | string | No | Specifies the start time of the aggregate (OHLC) candles you want returned (YYYY-MM-DD date or nanosecond Unix timestamp). How it works - If not provided, the API returns the most recent candles available, up to the limit you set. - If provided, the value determines which candle(s) to return. The timestamp or date is “snapped” to the start time of the matching candle interval. - You can use comparison operators to form ranges:   - `window_start.gte` – greater than or equal to   - `window_start.gt` – greater than   - `window_start.lte` – less than or equal to   - `window_start.lt` – less than  Examples 1. Most recent minute candles    `/vX/aggs/ESU5?resolution=1min&limit=5`  2. Daily candle for August 5, 2025    `/vX/aggs/ESU5?resolution=1day&window_start=2025-08-05`  3. Daily candles from July 1–31, 2025    `/vX/aggs/ESU5?resolution=1day&window_start.gte=2025-07-01&window_start.lte=2025-07-31`  4. 1,000 one-second candles after a specific timestamp    `/vX/aggs/ESU5?resolution=1sec&window_start.gt=1751409877000000000&limit=1000` |
| `limit` | integer | No | The number of results to return per page (default=1000, maximum=50000, minimum=1). |
| `window_start.gte` | string | No | Range by window_start. |
| `window_start.gt` | string | No | Range by window_start. |
| `window_start.lte` | string | No | Range by window_start. |
| `window_start.lt` | string | No | Range by window_start. |
| `sort` | string | No | Sort results by field and direction using dotted notation (e.g., 'ticker.asc', 'name.desc'). |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, the URL to the next page of results. |
| `results` | array[object] | N/A |
| `results[].close` | number | The last price within the timeframe. |
| `results[].dollar_volume` | number | The total dollar volume of the transactions that occurred within the timeframe. |
| `results[].high` | number | The highest price within the timeframe. |
| `results[].low` | number | The lowest price within the timeframe. |
| `results[].open` | number | The opening price within the timeframe. |
| `results[].session_end_date` | string | Also known as the trading date, the date of the end of the trading session, in YYYY-MM-DD format. |
| `results[].settlement_price` | number | The price the contract would have cost to settle for this session. |
| `results[].ticker` | string | The ticker for the contract. |
| `results[].transactions` | integer | The number of transactions that occurred within the timeframe. |
| `results[].volume` | integer | The number of contracts that traded within the timeframe. |
| `results[].window_start` | integer | The timestamp of the beginning of the candlestick’s aggregation window. |
| `status` | string | The status of the response. |

## Sample Response

```json
{
  "request_id": "b452e45b7eaad14151c3e1ce5129b558",
  "results": [
    {
      "close": 2874.2,
      "dollar_volume": 380560636.01,
      "high": 2877.1,
      "low": 2837.4,
      "open": 2849.8,
      "session_end_date": "2025-02-04",
      "settlement_price": 2875.8,
      "ticker": "GCJ5",
      "transactions": 74223,
      "volume": 133072,
      "window_start": 1738627200000000000
    },
    {
      "close": 2884.8,
      "dollar_volume": 448429944.1,
      "high": 2906,
      "low": 2870.1,
      "open": 2873.7,
      "session_end_date": "2025-02-05",
      "settlement_price": 2893,
      "ticker": "GCJ5",
      "transactions": 83673,
      "volume": 155170,
      "window_start": 1738713600000000000
    }
  ],
  "status": "OK"
}
```
