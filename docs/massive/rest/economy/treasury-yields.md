# REST
## Economy

### Treasury Yields

**Endpoint:** `GET /fed/v1/treasury-yields`

**Description:**

Retrieve historical U.S. Treasury yield data for standard timeframes ranging from 1-month to 30-years, with daily historical records back to 1962. This endpoint lets you query by date or date range to see how interest rates have changed over time. Each data point reflects the market yield for Treasury securities of a specific maturity, helping users understand short- and long-term rate movements.

Use Cases: Charting rate trends, comparing short vs. long-term yields, economic research.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `date` | string | No | Calendar date of the yield observation (YYYY-MM-DD). |
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
| `results[].date` | string | Calendar date of the yield observation (YYYY-MM-DD). |
| `results[].yield_10_year` | number | Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, Quoted on an Investment Basis |
| `results[].yield_1_month` | number | Market Yield on U.S. Treasury Securities at 1-Month Constant Maturity, Quoted on an Investment Basis |
| `results[].yield_1_year` | number | Market Yield on U.S. Treasury Securities at 1-Year Constant Maturity, Quoted on an Investment Basis |
| `results[].yield_20_year` | number | Market Yield on U.S. Treasury Securities at 20-Year Constant Maturity, Quoted on an Investment Basis |
| `results[].yield_2_year` | number | Market Yield on U.S. Treasury Securities at 2-Year Constant Maturity, Quoted on an Investment Basis |
| `results[].yield_30_year` | number | Market Yield on U.S. Treasury Securities at 30-Year Constant Maturity, Quoted on an Investment Basis |
| `results[].yield_3_month` | number | Market Yield on U.S. Treasury Securities at 3-Month Constant Maturity, Quoted on an Investment Basis |
| `results[].yield_3_year` | number | Market Yield on U.S. Treasury Securities at 3-Year Constant Maturity, Quoted on an Investment Basis |
| `results[].yield_5_year` | number | Market Yield on U.S. Treasury Securities at 5-Year Constant Maturity, Quoted on an Investment Basis |
| `results[].yield_6_month` | number | Market Yield on U.S. Treasury Securities at 6-Month Constant Maturity, Quoted on an Investment Basis |
| `results[].yield_7_year` | number | Market Yield on U.S. Treasury Securities at 7-Year Constant Maturity, Quoted on an Investment Basis |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "date": "1962-01-02",
      "yield_10_year": 4.06,
      "yield_1_year": 3.22,
      "yield_5_year": 3.88
    }
  ],
  "status": "OK"
}
```
