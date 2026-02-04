# REST
## Partners

### Earnings

**Endpoint:** `GET /benzinga/v1/earnings`

**Description:**

Retrieve structured historical and upcoming earnings announcements for publicly traded companies, including key metrics such as earnings per share (EPS), revenue, and analyst estimates. Each record includes reported values, estimated figures, and surprise metrics, with optional context like fiscal period, confirmation status, and event importance. Data is timestamped, filterable by ticker and date, and includes both the actual and prior-period values. This endpoint provides clean, developer-friendly access to earnings events for use in analytics, automation, or investor tools.

Use Cases: Earnings calendar tracking, financial modeling, event-driven alerts, market research.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `date` | string | No | The calendar date (formatted as YYYY-MM-DD) when the earnings are scheduled or were reported. |
| `date.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `date.gt` | string | No | Filter greater than the value. |
| `date.gte` | string | No | Filter greater than or equal to the value. |
| `date.lt` | string | No | Filter less than the value. |
| `date.lte` | string | No | Filter less than or equal to the value. |
| `ticker` | string | No | The stock symbol of the company reporting earnings. |
| `ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `ticker.gt` | string | No | Filter greater than the value. |
| `ticker.gte` | string | No | Filter greater than or equal to the value. |
| `ticker.lt` | string | No | Filter less than the value. |
| `ticker.lte` | string | No | Filter less than or equal to the value. |
| `importance` | integer | No | A subjective indicator of the importance of the event, on a scale from 0 (lowest) to 5 (highest). Value must be an integer. |
| `importance.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. Value must be an integer. |
| `importance.gt` | integer | No | Filter greater than the value. Value must be an integer. |
| `importance.gte` | integer | No | Filter greater than or equal to the value. Value must be an integer. |
| `importance.lt` | integer | No | Filter less than the value. Value must be an integer. |
| `importance.lte` | integer | No | Filter less than or equal to the value. Value must be an integer. |
| `last_updated` | string | No | The timestamp (formatted as an ISO 8601 timestamp) when the record was last updated in the system. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.gt` | string | No | Filter greater than the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.gte` | string | No | Filter greater than or equal to the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.lt` | string | No | Filter less than the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.lte` | string | No | Filter less than or equal to the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `date_status` | string | No | Indicates whether the date of the earnings report has been confirmed. Possible values include: projected, confirmed. |
| `date_status.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `date_status.gt` | string | No | Filter greater than the value. |
| `date_status.gte` | string | No | Filter greater than or equal to the value. |
| `date_status.lt` | string | No | Filter less than the value. |
| `date_status.lte` | string | No | Filter less than or equal to the value. |
| `eps_surprise_percent` | number | No | The percentage difference between the actual and estimated EPS. Value must be a floating point number. |
| `eps_surprise_percent.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. Value must be a floating point number. |
| `eps_surprise_percent.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `eps_surprise_percent.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `eps_surprise_percent.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `eps_surprise_percent.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `revenue_surprise_percent` | number | No | The percentage difference between the actual and estimated revenue. Value must be a floating point number. |
| `revenue_surprise_percent.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. Value must be a floating point number. |
| `revenue_surprise_percent.gt` | number | No | Filter greater than the value. Value must be a floating point number. |
| `revenue_surprise_percent.gte` | number | No | Filter greater than or equal to the value. Value must be a floating point number. |
| `revenue_surprise_percent.lt` | number | No | Filter less than the value. Value must be a floating point number. |
| `revenue_surprise_percent.lte` | number | No | Filter less than or equal to the value. Value must be a floating point number. |
| `fiscal_year` | integer | No | The fiscal year in which the earnings period falls. Value must be an integer. |
| `fiscal_year.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. Value must be an integer. |
| `fiscal_year.gt` | integer | No | Filter greater than the value. Value must be an integer. |
| `fiscal_year.gte` | integer | No | Filter greater than or equal to the value. Value must be an integer. |
| `fiscal_year.lt` | integer | No | Filter less than the value. Value must be an integer. |
| `fiscal_year.lte` | integer | No | Filter less than or equal to the value. Value must be an integer. |
| `fiscal_period` | string | No | The fiscal period for which the earnings are reported. Examples include: Q1, Q2, H1, FY. |
| `fiscal_period.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `fiscal_period.gt` | string | No | Filter greater than the value. |
| `fiscal_period.gte` | string | No | Filter greater than or equal to the value. |
| `fiscal_period.lt` | string | No | Filter less than the value. |
| `fiscal_period.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'last_updated' if not specified. The sort order defaults to 'desc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].actual_eps` | number | The actual earnings per share (EPS) reported by the company for the given period. |
| `results[].actual_revenue` | number | The actual revenue reported by the company for the given fiscal period. |
| `results[].benzinga_id` | string | The identifer used by Benzinga for this record. |
| `results[].company_name` | string | The name of the company releasing earnings. |
| `results[].currency` | string | The ISO 4217 currency code indicating the denomination in which the figures are reported. |
| `results[].date` | string | The calendar date (formatted as YYYY-MM-DD) when the earnings are scheduled or were reported. |
| `results[].date_status` | string | Indicates whether the date of the earnings report has been confirmed. Possible values include: projected, confirmed. |
| `results[].eps_method` | string | The methodology of the EPS figure. Possible values are gaap (standardized financials under Generally Accepted Accounting Principles), ffo (Funds From Operations, a non-GAAP metric commonly used to assess the operating performance of REITs), and adj (adjusted, non-GAAP). |
| `results[].eps_surprise` | number | The difference between the actual and estimated EPS. |
| `results[].eps_surprise_percent` | number | The percentage difference between the actual and estimated EPS. |
| `results[].estimated_eps` | number | The analyst consensus estimate for earnings per share (EPS) for the given period. |
| `results[].estimated_revenue` | number | The analyst consensus estimate for the company's revenue in the given period. |
| `results[].fiscal_period` | string | The fiscal period for which the earnings are reported. Examples include: Q1, Q2, H1, FY. |
| `results[].fiscal_year` | integer | The fiscal year in which the earnings period falls. |
| `results[].importance` | integer | A subjective indicator of the importance of the event, on a scale from 0 (lowest) to 5 (highest). |
| `results[].last_updated` | string | The timestamp (formatted as an ISO 8601 timestamp) when the record was last updated in the system. |
| `results[].notes` | string | Additional context, commentary, or clarifying notes related to the earnings event. |
| `results[].previous_eps` | number | The company's reported earnings per share (EPS) for the previous comparable period. |
| `results[].previous_revenue` | number | The company's revenue for the previous comparable fiscal period. |
| `results[].revenue_method` | string | The methodology of the revenue figure. Possible values are gaap (standardized financials under Generally Accepted Accounting Principles), adj (adjusted, non-GAAP figures that exclude certain items like one-time charges or divestitures), and rental (revenue specifically derived from rental operations, typically used by REITs, leasing companies, or businesses with a rental-based model). |
| `results[].revenue_surprise` | number | The difference between the actual and estimated revenue. |
| `results[].revenue_surprise_percent` | number | The percentage difference between the actual and estimated revenue. |
| `results[].ticker` | string | The stock symbol of the company reporting earnings. |
| `results[].time` | string | The time (formatted as 24-hour HH:MM:SS UTC) when the earnings are scheduled or were reported. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "actual_eps": -0.19,
      "actual_revenue": 17566000,
      "benzinga_id": "651bcd8d7e4d6000011f2232",
      "company_name": "Guardforce AI Co",
      "currency": "USD",
      "date": "2029-09-24",
      "date_status": "confirmed",
      "eps_method": "GAAP",
      "fiscal_period": "H1",
      "fiscal_year": 2024,
      "importance": 1,
      "last_updated": "2025-01-07T10:19:50Z",
      "previous_eps": -0.75,
      "previous_revenue": 18413292,
      "revenue_method": "GAAP",
      "ticker": "GFAI",
      "time": "07:00:00"
    }
  ],
  "status": "OK"
}
```
