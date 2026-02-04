# REST
## Economy

### Inflation

**Endpoint:** `GET /fed/v1/inflation`

**Description:**

Retrieve key indicators of realized inflation, reflecting actual changes in consumer prices and spending behavior in the U.S. economy. This endpoint provides both headline and core inflation measures from the CPI and PCE indexes, offering a well-rounded view of historical price trends.

Use Cases: Tracking inflation trends, evaluating purchasing power, supporting monetary policy and economic research.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `date` | string | No | Calendar date of the observation (YYYY‑MM‑DD). |
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
| `results[].cpi` | number | Consumer Price Index (CPI) for All Urban Consumers — a standard measure of headline inflation based on a fixed basket of goods and services, not seasonally adjusted. |
| `results[].cpi_core` | number | Core Consumer Price Index — the CPI excluding food and energy, used to understand underlying inflation trends without short-term volatility. |
| `results[].cpi_year_over_year` | number | Year-over-year percentage change in the headline CPI — the most commonly cited inflation rate in public discourse and economic policy. |
| `results[].date` | string | Calendar date of the observation (YYYY‑MM‑DD). |
| `results[].pce` | number | Personal Consumption Expenditures (PCE) Price Index — a broader measure of inflation used by the Federal Reserve, reflecting actual consumer spending patterns and updated basket weights. |
| `results[].pce_core` | number | Core PCE Price Index — excludes food and energy prices from the PCE index, and is the Fed's preferred measure of underlying inflation. |
| `results[].pce_spending` | number | Nominal Personal Consumption Expenditures — total dollar value of consumer spending in the U.S. economy, reported in billions of dollars and not adjusted for inflation. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "cpi": 310.45,
      "cpi_core": 320.1,
      "cpi_year_over_year": 3.18,
      "date": "2025-06-01",
      "pce": 132.73,
      "pce_core": 131.9,
      "pce_spending": 20345.6
    }
  ],
  "status": "OK"
}
```
