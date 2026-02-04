# REST
## Futures

### Market Status

**Endpoint:** `GET /futures/vX/market-status`

**Description:**

Retrieve the current market status for a specific product or products. This endpoint returns real-time indicators, such as open, pause, close, for futures products, along with the corresponding exchange and product codes and an evaluation timestamp. This information enables users to monitor operational conditions and adjust their trading strategies accordingly.

Use Cases: Real-time monitoring, algorithm scheduling, UI updates, operational planning.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `product_code` | string | No | The product code of the futures contracts for which you want statuses. |
| `product_code.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `product_code.gt` | string | No | Filter greater than the value. |
| `product_code.gte` | string | No | Filter greater than or equal to the value. |
| `product_code.lt` | string | No | Filter less than the value. |
| `product_code.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].market_event` | string | The current status of the market for the product. |
| `results[].name` | string | The name of the futures product. |
| `results[].product_code` | string | The product code of the futures contracts for which you want statuses. |
| `results[].session_end_date` | string | The trading date for the current session. |
| `results[].timestamp` | string | The timestamp for the given market event. |
| `results[].trading_venue` | string | The trading venue (MIC) for the exchange on which the corresponding product trades. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "request_id": "445ebfcfe5bb4b688b7971e1600c952d",
  "results": [
    {
      "market_event": "open",
      "name": "ERCOT North 345 kV Hub Day-Ahead 5 MW Off-Peak Futures",
      "product_code": "ERL",
      "session_end_date": "2025-12-05",
      "timestamp": "2025-12-04T23:00:00+00:00",
      "trading_venue": "XNYM"
    }
  ],
  "status": "OK"
}
```
