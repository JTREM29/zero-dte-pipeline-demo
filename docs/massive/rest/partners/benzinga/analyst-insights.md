# REST
## Partners

### Analyst Insights

**Endpoint:** `GET /benzinga/v1/analyst-insights`

**Description:**

Retrieve insights from financial analysts, including ratings, price targets, and the rationale behind their recommendations. Each record captures key drivers such as valuation metrics, strategic initiatives, and sector positioning. This data offers a structured view of analyst sentiment over time and supports deeper analysis of company outlook and market expectations.

Use Cases: Analyst sentiment tracking, investment research, valuation benchmarking.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `date` | string | No | The calendar date (formatted as YYYY-MM-DD) when the rating was issued. |
| `date.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `date.gt` | string | No | Filter greater than the value. |
| `date.gte` | string | No | Filter greater than or equal to the value. |
| `date.lt` | string | No | Filter less than the value. |
| `date.lte` | string | No | Filter less than or equal to the value. |
| `ticker` | string | No | The stock symbol of the company being rated. |
| `ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `ticker.gt` | string | No | Filter greater than the value. |
| `ticker.gte` | string | No | Filter greater than or equal to the value. |
| `ticker.lt` | string | No | Filter less than the value. |
| `ticker.lte` | string | No | Filter less than or equal to the value. |
| `last_updated` | string | No | The timestamp (formatted as an ISO 8601 timestamp) when the rating was last updated in the system. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.gt` | string | No | Filter greater than the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.gte` | string | No | Filter greater than or equal to the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.lt` | string | No | Filter less than the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.lte` | string | No | Filter less than or equal to the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `firm` | string | No | The name of the research firm or investment bank issuing the rating. |
| `firm.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `firm.gt` | string | No | Filter greater than the value. |
| `firm.gte` | string | No | Filter greater than or equal to the value. |
| `firm.lt` | string | No | Filter less than the value. |
| `firm.lte` | string | No | Filter less than or equal to the value. |
| `rating_action` | string | No | The description of the change in rating from the firm's last rating. Possible values include: downgrades, maintains, reinstates, reiterates, upgrades, assumes, initiates_coverage_on, terminates_coverage_on, removes, suspends, firm_dissolved. |
| `rating_action.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `rating_action.gt` | string | No | Filter greater than the value. |
| `rating_action.gte` | string | No | Filter greater than or equal to the value. |
| `rating_action.lt` | string | No | Filter less than the value. |
| `rating_action.lte` | string | No | Filter less than or equal to the value. |
| `benzinga_firm_id` | string | No | The identifer used by Benzinga for the firm record. |
| `benzinga_firm_id.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `benzinga_firm_id.gt` | string | No | Filter greater than the value. |
| `benzinga_firm_id.gte` | string | No | Filter greater than or equal to the value. |
| `benzinga_firm_id.lt` | string | No | Filter less than the value. |
| `benzinga_firm_id.lte` | string | No | Filter less than or equal to the value. |
| `benzinga_rating_id` | string | No | The identifier used by Benzinga for the rating record. |
| `benzinga_rating_id.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `benzinga_rating_id.gt` | string | No | Filter greater than the value. |
| `benzinga_rating_id.gte` | string | No | Filter greater than or equal to the value. |
| `benzinga_rating_id.lt` | string | No | Filter less than the value. |
| `benzinga_rating_id.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'last_updated' if not specified. The sort order defaults to 'desc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].benzinga_firm_id` | string | The identifer used by Benzinga for the firm record. |
| `results[].benzinga_id` | string | The identifer used by Benzinga for this record. |
| `results[].benzinga_rating_id` | string | The identifier used by Benzinga for the rating record. |
| `results[].company_name` | string | The name of the company being rated. |
| `results[].date` | string | The calendar date (formatted as YYYY-MM-DD) when the rating was issued. |
| `results[].firm` | string | The name of the research firm or investment bank issuing the rating. |
| `results[].insight` | string | Narrative commentary or reasoning provided by the analyst or firm to explain the rating or price target. |
| `results[].last_updated` | string | The timestamp (formatted as an ISO 8601 timestamp) when the rating was last updated in the system. |
| `results[].price_target` | number | The current price target set by the analyst. |
| `results[].rating` | string | The current rating set by the analyst. |
| `results[].rating_action` | string | The description of the change in rating from the firm's last rating. Possible values include: downgrades, maintains, reinstates, reiterates, upgrades, assumes, initiates_coverage_on, terminates_coverage_on, removes, suspends, firm_dissolved. |
| `results[].ticker` | string | The stock symbol of the company being rated. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "benzinga_firm_id": "606af0aa6538960001bced21",
      "benzinga_id": "681363c1fd0258abcbedc074",
      "benzinga_rating_id": "6813624c09c1f6000103ac25",
      "date": "2025-05-01",
      "firm": "Needham",
      "insight": "Needham maintained their Buy rating on Etsy's stock with a price target of $55.00.  \n\n **Growth Initiatives and Market Penetration**: Etsy's focus on growth initiatives, including leveraging its app for a more personalized shopping experience and marketing, has been a key factor in maintaining its Buy rating. The company's ability to drive greater consideration and purchase frequency through technology and product initiatives, alongside its significant app penetration of gross merchandise sales (GMS), showcases its strong position to capture more of the consumer wallet.\n\n**Resilience Amid Economic Uncertainty**: Despite the economic uncertainty, including potential impacts from tariffs, Etsy's asset-light model and strategic focus on product enhancements position it to navigate macro headwinds effectively. The company's efforts to lean into paid social channels for marketing and its ability to adapt to changes in consumer behavior underline its resilience and potential for sustained growth, supporting the Buy rating.",
      "last_updated": "2025-05-01T12:06:36Z",
      "price_target": 55,
      "rating": "buy",
      "rating_action": "maintains"
    }
  ],
  "status": "OK"
}
```
