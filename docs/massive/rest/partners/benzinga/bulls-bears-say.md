# REST
## Partners

### Bulls Bears Say

**Endpoint:** `GET /benzinga/v1/bulls-bears-say`

**Description:**

A comprehensive database of analyst bull and bear case summaries for publicly traded companies, providing concise summaries of both bullish and bearish investment arguments to help investors see both sides of the story before making investment decisions. Each entry includes the key points for and against investing in a particular stock.

Use Cases: Investment research, sentiment analysis, due diligence, portfolio evaluation, risk assessment.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | No | The stock ticker symbol for the company associated with the bull and bear case summaries. |
| `ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `ticker.gt` | string | No | Filter greater than the value. |
| `ticker.gte` | string | No | Filter greater than or equal to the value. |
| `ticker.lt` | string | No | Filter less than the value. |
| `ticker.lte` | string | No | Filter less than or equal to the value. |
| `benzinga_id` | string | No | The unique identifier used by Benzinga for this bull/bear case record. |
| `benzinga_id.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `benzinga_id.gt` | string | No | Filter greater than the value. |
| `benzinga_id.gte` | string | No | Filter greater than or equal to the value. |
| `benzinga_id.lt` | string | No | Filter less than the value. |
| `benzinga_id.lte` | string | No | Filter less than or equal to the value. |
| `last_updated` | string | No | The timestamp (formatted as an ISO 8601 timestamp) when the bull/bear case was last updated in the system. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.gt` | string | No | Filter greater than the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.gte` | string | No | Filter greater than or equal to the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.lt` | string | No | Filter less than the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.lte` | string | No | Filter less than or equal to the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '5000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'ticker' if not specified. The sort order defaults to 'desc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].bear_case` | string | A concise summary of the bearish investment thesis, highlighting potential risks, challenges, and reasons why the stock could decline in value. |
| `results[].benzinga_id` | string | The unique identifier used by Benzinga for this bull/bear case record. |
| `results[].bull_case` | string | A concise summary of the bullish investment thesis, highlighting positive aspects, growth opportunities, and reasons why the stock could appreciate in value. |
| `results[].last_updated` | string | The timestamp (formatted as an ISO 8601 timestamp) when the bull/bear case was last updated in the system. |
| `results[].ticker` | string | The stock ticker symbol for the company associated with the bull and bear case summaries. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "bear_case": "Apple faces increasing regulatory scrutiny globally, potential market saturation in core iPhone markets, and intense competition in emerging categories. Supply chain vulnerabilities and dependence on China for manufacturing pose significant risks, while slowing innovation cycles could impact premium pricing.",
      "benzinga_id": "550e8400-e29b-41d4-a716-446655440000",
      "bull_case": "Apple's strong ecosystem integration, loyal customer base, and continued innovation in services and hardware drive sustainable revenue growth. The company's expanding services segment provides high-margin recurring revenue, while its brand strength and pricing power maintain premium market positioning.",
      "last_updated": "2025-12-16T10:30:00Z",
      "ticker": "AAPL"
    }
  ],
  "status": "OK"
}
```
