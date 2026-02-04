# REST
## Partners

### Analyst Details

**Endpoint:** `GET /benzinga/v1/analysts`

**Description:**

Retrieve structured data on financial analysts, including names, affiliated firms, and historical rating activity. Each record includes performance metrics such as success rate, average return, and percentile rankings. This data provides transparency into the analysts behind equity ratings and enables deeper evaluation of their track records.

Use Cases: Analyst performance tracking, research attribution, signal quality assessment.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `benzinga_id` | string | No | The identifier used by Benzinga for this record. |
| `benzinga_id.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `benzinga_id.gt` | string | No | Filter greater than the value. |
| `benzinga_id.gte` | string | No | Filter greater than or equal to the value. |
| `benzinga_id.lt` | string | No | Filter less than the value. |
| `benzinga_id.lte` | string | No | Filter less than or equal to the value. |
| `benzinga_firm_id` | string | No | The unique identifier assigned by Benzinga to the research firm or investment bank. |
| `benzinga_firm_id.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `benzinga_firm_id.gt` | string | No | Filter greater than the value. |
| `benzinga_firm_id.gte` | string | No | Filter greater than or equal to the value. |
| `benzinga_firm_id.lt` | string | No | Filter less than the value. |
| `benzinga_firm_id.lte` | string | No | Filter less than or equal to the value. |
| `firm_name` | string | No | The name of the research firm or investment bank issuing the ratings. |
| `firm_name.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `firm_name.gt` | string | No | Filter greater than the value. |
| `firm_name.gte` | string | No | Filter greater than or equal to the value. |
| `firm_name.lt` | string | No | Filter less than the value. |
| `firm_name.lte` | string | No | Filter less than or equal to the value. |
| `full_name` | string | No | The full name of the analyst associated with the ratings. |
| `full_name.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `full_name.gt` | string | No | Filter greater than the value. |
| `full_name.gte` | string | No | Filter greater than or equal to the value. |
| `full_name.lt` | string | No | Filter less than the value. |
| `full_name.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'full_name' if not specified. The sort order defaults to 'asc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].benzinga_firm_id` | string | The unique identifier assigned by Benzinga to the research firm or investment bank. |
| `results[].benzinga_id` | string | The identifier used by Benzinga for this record. |
| `results[].firm_name` | string | The name of the research firm or investment bank issuing the ratings. |
| `results[].full_name` | string | The full name of the analyst associated with the ratings. |
| `results[].last_updated` | string | The timestamp (formatted as an ISO 8601 timestamp) when the analyst record was last updated in the system. |
| `results[].overall_avg_return` | number | The average percent price difference per rating since the date of recommendation. |
| `results[].overall_avg_return_percentile` | number | The analyst's percentile rank based on average return, relative to other analysts. |
| `results[].overall_success_rate` | number | The percentage of gain/loss ratings that resulted in a gain overall. |
| `results[].smart_score` | number | A weighted average of the total_ratings_percentile, overall_avg_return_percentile, and overall_success_rate. |
| `results[].total_ratings` | number | The total number of ratings issued by the analyst included in the performance calculation. |
| `results[].total_ratings_percentile` | number | The analyst's percentile rank based on the total number of ratings issued, relative to other analysts. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "benzinga_firm_id": "5e17143f7da4190001b2eaa6",
      "benzinga_id": "65eb18289b25ca0001b34332",
      "firm_name": "B of A Securities",
      "full_name": "Alice Xiao",
      "last_updated": "2025-05-19T04:31:12Z",
      "overall_avg_return": 12.48,
      "overall_avg_return_percentile": 66.53,
      "overall_success_rate": 100,
      "smart_score": 67.94,
      "total_ratings": 4,
      "total_ratings_percentile": 32.17
    }
  ],
  "status": "OK"
}
```
