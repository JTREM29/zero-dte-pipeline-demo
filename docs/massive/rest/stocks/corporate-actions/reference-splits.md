# REST
## Stocks

### Splits (Deprecated)

**Endpoint:** `GET /v3/reference/splits`

**Description:**

Retrieve historical stock split events, including execution dates and ratio factors, to understand changes in a company’s share structure over time. Massive leverages this data for accurate price adjustments in other endpoints, such as the Aggregates API, ensuring that users can access both adjusted and unadjusted views of historical prices for more informed analysis.

Use Cases: Historical analysis, price adjustments, data consistency, modeling.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | No | Specify a case-sensitive ticker symbol. For example, AAPL represents Apple Inc. |
| `execution_date` | string | No | Query by execution date with the format YYYY-MM-DD. |
| `reverse_split` | boolean | No | Query for reverse stock splits. A split ratio where split_from is greater than split_to represents a reverse split. By default this filter is not used. |
| `ticker.gte` | string | No | Range by ticker. |
| `ticker.gt` | string | No | Range by ticker. |
| `ticker.lte` | string | No | Range by ticker. |
| `ticker.lt` | string | No | Range by ticker. |
| `execution_date.gte` | string | No | Range by execution_date. |
| `execution_date.gt` | string | No | Range by execution_date. |
| `execution_date.lte` | string | No | Range by execution_date. |
| `execution_date.lt` | string | No | Range by execution_date. |
| `order` | string | No | Order results based on the `sort` field. |
| `limit` | integer | No | Limit the number of results returned, default is 10 and max is 1000. |
| `sort` | string | No | Sort field used for ordering. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page of data. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].execution_date` | string | The execution date of the stock split. On this date the stock split was applied. |
| `results[].id` | string | The unique identifier for this stock split. |
| `results[].split_from` | number | The second number in the split ratio.  For example: In a 2-for-1 split, split_from would be 1. |
| `results[].split_to` | number | The first number in the split ratio.  For example: In a 2-for-1 split, split_to would be 2. |
| `results[].ticker` | string | The ticker symbol of the stock split. |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "next_url": "https://api.massive.com/v3/splits/AAPL?cursor=YWN0aXZlPXRydWUmZGF0ZT0yMDIxLTA0LTI1JmxpbWl0PTEmb3JkZXI9YXNjJnBhZ2VfbWFya2VyPUElN0M5YWRjMjY0ZTgyM2E1ZjBiOGUyNDc5YmZiOGE1YmYwNDVkYzU0YjgwMDcyMWE2YmI1ZjBjMjQwMjU4MjFmNGZiJnNvcnQ9dGlja2Vy",
  "request_id": "6a7e466379af0a71039d60cc78e72282",
  "results": [
    {
      "execution_date": "2020-08-31",
      "id": "E36416cce743c3964c5da63e1ef1626c0aece30fb47302eea5a49c0055c04e8d0",
      "split_from": 1,
      "split_to": 4,
      "ticker": "AAPL"
    },
    {
      "execution_date": "2005-02-28",
      "id": "E90a77bdf742661741ed7c8fc086415f0457c2816c45899d73aaa88bdc8ff6025",
      "split_from": 1,
      "split_to": 2,
      "ticker": "AAPL"
    }
  ],
  "status": "OK"
}
```
