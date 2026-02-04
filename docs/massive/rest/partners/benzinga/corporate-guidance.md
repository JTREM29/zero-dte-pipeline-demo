# REST
## Partners

### Corporate Guidance

**Endpoint:** `GET /benzinga/v1/guidance`

**Description:**

Retrieve structured earnings guidance data, including projected EPS and revenue figures, from public company disclosures. Each record includes key attributes such as the guidance date, fiscal period, and release type, with prior guidance values included when available. Data can be filtered by ticker, fiscal period, and date range. Guidance records are timestamped and sortable for flexible integration into downstream applications.

Use Cases: Market sentiment analysis, investment strategy development, financial modeling.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `date` | string | No | The calendar date (formatted as YYYY-MM-DD) when the guidance was issued. |
| `date.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `date.gt` | string | No | Filter greater than the value. |
| `date.gte` | string | No | Filter greater than or equal to the value. |
| `date.lt` | string | No | Filter less than the value. |
| `date.lte` | string | No | Filter less than or equal to the value. |
| `ticker` | string | No | The stock symbol of the company issuing guidance. |
| `ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `ticker.gt` | string | No | Filter greater than the value. |
| `ticker.gte` | string | No | Filter greater than or equal to the value. |
| `ticker.lt` | string | No | Filter less than the value. |
| `ticker.lte` | string | No | Filter less than or equal to the value. |
| `positioning` | string | No | Indicates how a particular guidance value is presented relative to other figures disclosed by the company. Possible values are 'primary' (the emphasized figure) and 'secondary' (a supporting or alternate figure) |
| `positioning.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `positioning.gt` | string | No | Filter greater than the value. |
| `positioning.gte` | string | No | Filter greater than or equal to the value. |
| `positioning.lt` | string | No | Filter less than the value. |
| `positioning.lte` | string | No | Filter less than or equal to the value. |
| `importance` | integer | No | A subjective indicator of the importance of the event, on a scale from 0 (lowest) to 5 (highest). Value must be an integer. |
| `importance.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. Value must be an integer. |
| `importance.gt` | integer | No | Filter greater than the value. Value must be an integer. |
| `importance.gte` | integer | No | Filter greater than or equal to the value. Value must be an integer. |
| `importance.lt` | integer | No | Filter less than the value. Value must be an integer. |
| `importance.lte` | integer | No | Filter less than or equal to the value. Value must be an integer. |
| `last_updated` | string | No | The timestamp (formatted as an ISO 8601 timestamp) when the record was last updated in the system. |
| `last_updated.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `last_updated.gt` | string | No | Filter greater than the value. |
| `last_updated.gte` | string | No | Filter greater than or equal to the value. |
| `last_updated.lt` | string | No | Filter less than the value. |
| `last_updated.lte` | string | No | Filter less than or equal to the value. |
| `fiscal_year` | integer | No | The fiscal year corresponding to the period for which the guidance is issued. Value must be an integer. |
| `fiscal_year.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. Value must be an integer. |
| `fiscal_year.gt` | integer | No | Filter greater than the value. Value must be an integer. |
| `fiscal_year.gte` | integer | No | Filter greater than or equal to the value. Value must be an integer. |
| `fiscal_year.lt` | integer | No | Filter less than the value. Value must be an integer. |
| `fiscal_year.lte` | integer | No | Filter less than or equal to the value. Value must be an integer. |
| `fiscal_period` | string | No | The fiscal quarter to which the guidance applies, such as Q1, Q2, Q3, or Q4. |
| `fiscal_period.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `fiscal_period.gt` | string | No | Filter greater than the value. |
| `fiscal_period.gte` | string | No | Filter greater than or equal to the value. |
| `fiscal_period.lt` | string | No | Filter less than the value. |
| `fiscal_period.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'date' if not specified. The sort order defaults to 'desc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].benzinga_id` | string | A unique identifier assigned by Benzinga to the guidance record. |
| `results[].company_name` | string | The name of the company issuing guidance. |
| `results[].currency` | string | The ISO 4217 code representing the currency in which the company issued its guidance figures. |
| `results[].date` | string | The calendar date (formatted as YYYY-MM-DD) when the guidance was issued. |
| `results[].eps_method` | string | The methodology of the EPS figure. Possible values are gaap (standardized financials under Generally Accepted Accounting Principles), ffo (Funds From Operations, a non-GAAP metric commonly used to assess the operating performance of REITs), and adj (adjusted, non-GAAP). |
| `results[].estimated_eps_guidance` | number | The midpoint or central earnings per share (EPS) value the company expects for the given fiscal period. |
| `results[].estimated_revenue_guidance` | number | The midpoint or central revenue figure the company expects for the given fiscal period. |
| `results[].fiscal_period` | string | The fiscal quarter to which the guidance applies, such as Q1, Q2, Q3, or Q4. |
| `results[].fiscal_year` | integer | The fiscal year corresponding to the period for which the guidance is issued. |
| `results[].importance` | integer | A subjective indicator of the importance of the event, on a scale from 0 (lowest) to 5 (highest). |
| `results[].last_updated` | string | The timestamp (formatted as an ISO 8601 timestamp) when the record was last updated in the system. |
| `results[].max_eps_guidance` | number | The highest EPS value the company expects for the fiscal period if a range was provided. |
| `results[].max_revenue_guidance` | number | The highest revenue figure the company expects for the fiscal period if a range was provided. |
| `results[].min_eps_guidance` | number | The lowest EPS value the company expects for the fiscal period if a range was provided. |
| `results[].min_revenue_guidance` | number | The lowest revenue figure the company expects for the fiscal period if a range was provided. |
| `results[].notes` | string | Additional descriptive text or commentary provided about the guidance record. |
| `results[].positioning` | string | Indicates how a particular guidance value is presented relative to other figures disclosed by the company. Possible values are 'primary' (the emphasized figure) and 'secondary' (a supporting or alternate figure) |
| `results[].previous_max_eps_guidance` | number | The highest EPS value issued in a previous guidance record for the same fiscal period. |
| `results[].previous_max_revenue_guidance` | number | The highest revenue value issued in a previous guidance record for the same fiscal period. |
| `results[].previous_min_eps_guidance` | number | The lowest EPS value issued in a previous guidance record for the same fiscal period. |
| `results[].previous_min_revenue_guidance` | number | The lowest revenue value issued in a previous guidance record for the same fiscal period. |
| `results[].release_type` | string | Indicates whether the guidance was issued as part of a scheduled earnings release ('official') or as an unscheduled update ('preliminary'). |
| `results[].revenue_method` | string | The methodology of the revenue figure. Possible values are gaap (standardized financials under Generally Accepted Accounting Principles) and adj (adjusted, non-GAAP). |
| `results[].ticker` | string | The stock symbol of the company issuing guidance. |
| `results[].time` | string | The time of day the guidance was announced, in HH:mm:ss format. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "benzinga_id": "682b28a9c068240001a9fded",
      "company_name": "Bath & Body Works",
      "currency": "USD",
      "date": "2025-05-19",
      "eps_method": "adj",
      "estimated_eps_guidance": 3.52,
      "estimated_revenue_guidance": 7470000000,
      "fiscal_period": "FY",
      "fiscal_year": 2025,
      "importance": 3,
      "last_updated": "2025-05-19T12:54:04Z",
      "max_eps_guidance": 3.6,
      "max_revenue_guidance": 7526000000,
      "min_eps_guidance": 3.25,
      "min_revenue_guidance": 7380000000,
      "notes": "Revenue for 2025 FY is expected to up by 1 to 3% YoY from $7.307B",
      "positioning": "primary",
      "previous_max_eps_guidance": 3.6,
      "previous_max_revenue_guidance": 7526000000,
      "previous_min_eps_guidance": 3.25,
      "previous_min_revenue_guidance": 7380000000,
      "release_type": "preliminary",
      "revenue_method": "gaap",
      "ticker": "BBWI",
      "time": "08:30:00"
    }
  ],
  "status": "OK"
}
```
