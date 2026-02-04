# REST
## Economy

### Labor Market

**Endpoint:** `GET /fed/v1/labor-market`

**Description:**

Retrieve key labor market indicators from the Federal Reserve, including unemployment rate, labor force participation, average hourly earnings, and job openings data.

Use Cases: Analyzing employment trends, monitoring workforce dynamics, supporting macroeconomic research and labor market analysis.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `date` | string | No | Calendar date of the observation (YYYY-MM-DD). |
| `date.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `date.gt` | string | No | Filter greater than the value. |
| `date.gte` | string | No | Filter greater than or equal to the value. |
| `date.lt` | string | No | Filter less than the value. |
| `date.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'date' if not specified. The sort order defaults to 'asc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].avg_hourly_earnings` | number | Average hourly earnings of all employees on private nonfarm payrolls in USD (CES0500000003 series from FRED). |
| `results[].date` | string | Calendar date of the observation (YYYY-MM-DD). |
| `results[].job_openings` | number | Total nonfarm job openings in thousands (JTSJOL series from FRED). |
| `results[].labor_force_participation_rate` | number | Civilian labor force participation rate as a percentage of the civilian noninstitutional population (CIVPART series from FRED). |
| `results[].unemployment_rate` | number | Civilian unemployment rate as a percentage of the labor force (UNRATE series from FRED). |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "avg_hourly_earnings": 35.06,
      "date": "2024-12-01",
      "job_openings": 8098,
      "labor_force_participation_rate": 62.5,
      "unemployment_rate": 4.2
    }
  ],
  "status": "OK"
}
```
