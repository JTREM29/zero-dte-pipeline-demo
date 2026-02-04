# REST
## Futures

### Schedules

**Endpoint:** `GET /futures/vX/schedules`

**Description:**

The Schedules API provides a unified way to retrieve trading schedules for futures markets, returning precise session open and close times, intraday breaks, and any adjustments for holidays or special events. You can request the full set of schedules for all products on a specific trading date or retrieve the schedule for a single product using its product code. All times are returned in Coordinated Universal Time (UTC), making it straightforward to align trading, execution, and operational workflows across systems.

Use Cases: Schedule planning, market analysis, strategy alignment, risk and operations management.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `product_code` | string | No | The product code of the futures contract. |
| `product_code.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `product_code.gt` | string | No | Filter greater than the value. |
| `product_code.gte` | string | No | Filter greater than or equal to the value. |
| `product_code.lt` | string | No | Filter less than the value. |
| `product_code.lte` | string | No | Filter less than or equal to the value. |
| `session_end_date` | string | No | The session end date for the schedules (also known as the trading date). This is the day in CT for which the user wants to retrieve data. If left blank, this value defaults to 'today' in Central Time. e.g. If a request is made from Pacific Time on '2025-01-01' at 11:00 pm with no 'session_end_date' a default value of `2025-01-02` will be used. |
| `session_end_date.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `session_end_date.gt` | string | No | Filter greater than the value. |
| `session_end_date.gte` | string | No | Filter greater than or equal to the value. |
| `session_end_date.lt` | string | No | Filter less than the value. |
| `session_end_date.lte` | string | No | Filter less than or equal to the value. |
| `trading_venue` | string | No | The trading venue (MIC) for the exchange on which this schedule's product trades. |
| `trading_venue.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `trading_venue.gt` | string | No | Filter greater than the value. |
| `trading_venue.gte` | string | No | Filter greater than or equal to the value. |
| `trading_venue.lt` | string | No | Filter less than the value. |
| `trading_venue.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '10' if not specified. The maximum allowed limit is '1000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'product_code' if not specified. The sort order defaults to 'asc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].event` | string | The type of session on the given trading date. |
| `results[].product_code` | string | The product code of the futures contract. |
| `results[].product_name` | string | The name of the futures product to which this schedule applies. |
| `results[].session_end_date` | string | The session end date for the schedules (also known as the trading date). This is the day in CT for which the user wants to retrieve data. If left blank, this value defaults to 'today' in Central Time. e.g. If a request is made from Pacific Time on '2025-01-01' at 11:00 pm with no 'session_end_date' a default value of `2025-01-02` will be used. |
| `results[].timestamp` | string | The timestamp for the given market event. |
| `results[].trading_venue` | string | The trading venue (MIC) for the exchange on which this schedule's product trades. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "next_url": "https://api.staging.massive.com/futures/vX/schedules?cursor=AQANA0VSTAIAAAEFAAEBAwACAQ0DRVJMAQ0ZMjAyNC0wNi0xMFQyMTowMDowMCswMDowMA==",
  "request_id": "a83620d1ec6a4cd5b84ea669e377fd47",
  "results": [
    {
      "event": "pre_open",
      "product_code": "ERL",
      "product_name": "ERCOT North 345 kV Hub Day-Ahead 5 MW Off-Peak Futures",
      "session_end_date": "2024-06-10",
      "timestamp": "2024-06-09T21:00:00+00:00",
      "trading_venue": "XNYM"
    },
    {
      "event": "open",
      "product_code": "ERL",
      "product_name": "ERCOT North 345 kV Hub Day-Ahead 5 MW Off-Peak Futures",
      "session_end_date": "2024-06-10",
      "timestamp": "2024-06-09T22:00:00+00:00",
      "trading_venue": "XNYM"
    },
    {
      "event": "close",
      "product_code": "ERL",
      "product_name": "ERCOT North 345 kV Hub Day-Ahead 5 MW Off-Peak Futures",
      "session_end_date": "2024-06-10",
      "timestamp": "2024-06-10T21:00:00+00:00",
      "trading_venue": "XNYM"
    }
  ],
  "status": "OK"
}
```
