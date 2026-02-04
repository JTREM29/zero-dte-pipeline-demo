# REST
## Futures

### Futures Contracts Snapshot

**Endpoint:** `GET /futures/vX/snapshot`

**Description:**

Retrieve real-time snapshots for a set of futures contracts, including key market data such as the latest trade, quote, session metrics (open, high, low, close, volume), and settlement prices. This endpoint returns the most up-to-date view of contract activity, filtered by ticker or product code, and supports custom sorting and pagination for efficient data access.

Use Cases: Real-time trading systems, intraday market analysis, performance monitoring, and portfolio valuation.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `product_code` | string | No | The code for the contracts' underlying product. |
| `product_code.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `product_code.gt` | string | No | Filter greater than the value. |
| `product_code.gte` | string | No | Filter greater than or equal to the value. |
| `product_code.lt` | string | No | Filter less than the value. |
| `product_code.lte` | string | No | Filter less than or equal to the value. |
| `ticker` | string | No | The futures contract identifier, including the base symbol and contract expiration (e.g., ESZ24 for the December 2024 S&P 500 E-mini contract). |
| `ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `ticker.gt` | string | No | Filter greater than the value. |
| `ticker.gte` | string | No | Filter greater than or equal to the value. |
| `ticker.lt` | string | No | Filter less than the value. |
| `ticker.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'ticker' if not specified. The sort order defaults to 'asc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].details` | object | N/A |
| `results[].last_minute` | object | N/A |
| `results[].last_quote` | object | N/A |
| `results[].last_trade` | object | N/A |
| `results[].session` | object | N/A |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "next_url": "https://api.massive.com/v1/futures/vX/snapshot?cursor=AQANAkNCBVNQ",
  "request_id": "c4d50f4801874e30b63e674b844cf51f",
  "results": [
    {
      "details": {
        "open_interest": 112,
        "settlement_date": 1753851600000000000
      },
      "last_minute": {
        "close": 240.00000000000003,
        "high": 240.00000000000003,
        "last_updated": 1746045300000,
        "low": 240.00000000000003,
        "open": 240.00000000000003,
        "volume": 5
      },
      "last_quote": {
        "ask": 240.50000000000003,
        "ask_size": 3,
        "ask_timestamp": 1746036204386194000,
        "bid": 239.50000000000003,
        "bid_size": 2,
        "bid_timestamp": 1746035747932118000,
        "last_updated": 1746046798024234200
      },
      "last_trade": {
        "last_updated": 1746045242858242600,
        "price": 240.00000000000003,
        "size": 5
      },
      "product_code": "CB",
      "session": {
        "change": 21.11,
        "change_percent": 0.09622134,
        "close": 240.00000000000003,
        "high": 241.00000000000003,
        "low": 240.00000000000003,
        "open": 240.00000000000003,
        "previous_settlement": 219.39,
        "settlement_price": 240.50000000000003,
        "volume": 55
      },
      "ticker": "CBN5"
    }
  ],
  "status": "OK"
}
```
