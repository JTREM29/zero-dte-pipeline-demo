# REST
## Partners

### ETF Fund Flows

**Endpoint:** `GET /etf-global/v1/fund-flows`

**Description:**

Track capital movements and investor activity across global ETFs. Access fund flow data that reveals market trends, investor sentiment, and the popularity of different ETF strategies over time.

Use Cases: Investor sentiment tracking, ETF inflow/outflow analysis, market trend monitoring, asset allocation strategy evaluation.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `processed_date` | string | No | The date showing when ETF Global received and processed the data. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date` | string | No | The date showing when the information was accurate or valid; some issuers, such as Vanguard, release their data on a delay, so the effective_date can be several weeks earlier than the processed_date. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `composite_ticker` | string | No | The stock ticker symbol used to identify this ETF on exchanges. |
| `composite_ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `composite_ticker.gt` | string | No | Filter greater than the value. |
| `composite_ticker.gte` | string | No | Filter greater than or equal to the value. |
| `composite_ticker.lt` | string | No | Filter less than the value. |
| `composite_ticker.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '5000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'composite_ticker' if not specified. The sort order defaults to 'asc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].composite_ticker` | string | The stock ticker symbol used to identify this ETF on exchanges. |
| `results[].effective_date` | string | The date showing when the information was accurate or valid; some issuers, such as Vanguard, release their data on a delay, so the effective_date can be several weeks earlier than the processed_date. |
| `results[].fund_flow` | number | The net daily capital flow into or out of the ETF through the creation and redemption process, where positive values indicate inflows and negative values indicate outflows. |
| `results[].nav` | number | The net asset value per share, representing the per-share value of the ETF's underlying holdings. |
| `results[].processed_date` | string | The date showing when ETF Global received and processed the data. |
| `results[].shares_outstanding` | number | The total number of ETF shares currently issued and outstanding in the market. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "composite_ticker": "SPY",
      "effective_date": "2025-01-29",
      "fund_flow": -30235124.7,
      "nav": 601.877341,
      "processed_date": "2025-01-29",
      "shares_outstanding": 1047232116
    },
    {
      "composite_ticker": "SPY",
      "effective_date": "2025-01-30",
      "fund_flow": -2798729635.65,
      "nav": 605.0574,
      "processed_date": "2025-01-30",
      "shares_outstanding": 1042582116
    },
    {
      "composite_ticker": "SPY",
      "effective_date": "2025-01-31",
      "fund_flow": -3358068570,
      "nav": 602.044248,
      "processed_date": "2025-01-31",
      "shares_outstanding": 1037032116
    }
  ],
  "status": "OK"
}
```
