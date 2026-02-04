# REST
## Crypto

### Trades

**Endpoint:** `GET /v3/trades/{cryptoTicker}`

**Description:**

Retrieve comprehensive, tick-level trade data for a specified crypto ticker within a defined time range. Each record includes price, size, exchange, trade conditions, and precise timestamp information. This granular data is foundational for constructing aggregated bars and performing in-depth analyses, as it captures every trade that contributes to calculations of open, high, low, and close (OHLC) values. By leveraging these trades, users can refine their understanding of intraday price movements, test and optimize algorithmic strategies, and ensure compliance by maintaining an auditable record of market activity.

Use Cases: Intraday analysis, algorithmic trading, market microstructure research, data integrity and compliance.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `cryptoTicker` | string | Yes | The ticker symbol to get trades for. |
| `timestamp` | string | No | Query by trade timestamp. Either a date with the format YYYY-MM-DD or a nanosecond timestamp. |
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
| `results[].conditions` | array[integer] | A list of condition codes. |
| `results[].exchange` | integer | The exchange ID. See <a href="https://massive.com/docs/rest/crypto/market-operations/exchanges" alt="Exchanges">Exchanges</a> for Massive's mapping of exchange IDs. |
| `results[].id` | string | The Trade ID which uniquely identifies a trade on the exchange that the trade happened on. |
| `results[].participant_timestamp` | integer | The nanosecond Exchange Unix Timestamp. This is the timestamp of when the trade was generated at the exchange. |
| `results[].price` | number | The price of the trade in the base currency of the crypto pair. |
| `results[].size` | number | The size of a trade (also known as volume). |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "next_url": "https://api.massive.com/v3/trades/X:BTC-USD?cursor=YWN0aXZlPXRydWUmZGF0ZT0yMDIxLTA0LTI1JmxpbWl0PTEmb3JkZXI9YXNjJnBhZ2VfbWFya2VyPUElN0M5YWRjMjY0ZTgyM2E1ZjBiOGUyNDc5YmZiOGE1YmYwNDVkYzU0YjgwMDcyMWE2YmI1ZjBjMjQwMjU4MjFmNGZiJnNvcnQ9dGlja2Vy",
  "request_id": "a47d1beb8c11b6ae897ab76cdbbf35a3",
  "results": [
    {
      "conditions": [
        1
      ],
      "exchange": 1,
      "id": "191450340",
      "participant_timestamp": 1625097600103000000,
      "price": 35060,
      "size": 1.0434526
    },
    {
      "conditions": [
        2
      ],
      "exchange": 1,
      "id": "191450341",
      "participant_timestamp": 1625097600368000000,
      "price": 35059.99,
      "size": 0.0058883
    }
  ],
  "status": "OK"
}
```
