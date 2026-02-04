# REST
## Economy

### Inflation Expectations

**Endpoint:** `GET /fed/v1/inflation-expectations`

**Description:**

Retrieve a broad view of how inflation is expected to evolve over time in the U.S. economy. This endpoint combines signals from financial markets and economic models to capture both near-term and long-term inflation outlooks. Each data point helps contextualize how inflation risk is perceived by investors and forecasters across different time horizons.

Use Cases: Analyzing inflation sentiment, comparing near-term vs. long-term expectations, supporting macroeconomic research and fixed-income strategy.

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
| `results[].date` | string | Calendar date of the observation (YYYY‑MM‑DD). |
| `results[].forward_years_5_to_10` | number | 5-Year, 5-Year Forward Inflation Expectation Rate — the market's expectation of average annual inflation for the 5-year period beginning 5 years from now, based on the spread between forward nominal and real yields. |
| `results[].market_10_year` | number | 10-Year Breakeven Inflation Rate — the market's expectation of average annual inflation over the next 10 years, based on the spread between 10-year nominal Treasury yields and 10-year TIPS yields. |
| `results[].market_5_year` | number | 5-Year Breakeven Inflation Rate — the market's expectation of average annual inflation over the next 5 years, based on the spread between 5-year nominal Treasury yields and 5-year TIPS yields. |
| `results[].model_10_year` | number | The Cleveland Fed’s 10-year inflation expectations data estimated expected inflation, risk premiums, and the real interest rate using a model based on Treasury yields, inflation data, swaps, and surveys. |
| `results[].model_1_year` | number | The Cleveland Fed’s 1-year inflation expectations data estimated expected inflation, risk premiums, and the real interest rate using a model based on Treasury yields, inflation data, swaps, and surveys. |
| `results[].model_30_year` | number | The Cleveland Fed’s 30-year inflation expectations data estimated expected inflation, risk premiums, and the real interest rate using a model based on Treasury yields, inflation data, swaps, and surveys. |
| `results[].model_5_year` | number | The Cleveland Fed’s 5-year inflation expectations data estimated expected inflation, risk premiums, and the real interest rate using a model based on Treasury yields, inflation data, swaps, and surveys. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "date": "2025-06-17",
      "forward_years_5_to_10": 2.6,
      "market_10_year": 2.36,
      "market_5_year": 2.12,
      "model_10_year": 2.95,
      "model_1_year": 2.85,
      "model_30_year": 3,
      "model_5_year": 2.91
    }
  ],
  "status": "OK"
}
```
