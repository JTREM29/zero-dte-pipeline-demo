# REST
## Partners

### Analyst Ratings

**Endpoint:** `GET /benzinga/v1/ratings`

**Description:**

Retrieve structured historical analyst ratings, including rating actions, price target changes, and firm names for publicly traded companies. Each record captures key attributes such as the rating date, action type (e.g., downgrade, maintain), and firm issuing the rating, along with optional price target changes and surprise indicators. Data can be filtered by ticker, firm, rating action, and date range. Price targets are optionally adjusted for corporate actions like splits and dividends. Records are timestamped and sortable for flexible integration into downstream applications.

Use Cases: Market sentiment tracking, portfolio alerts, backtesting rating impact, trend analysis.

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
| `importance` | integer | No | A subjective indicator of the importance of the earnings event, on a scale from 0 (lowest) to 5 (highest). Value must be an integer. |
| `importance.gt` | integer | No | Filter greater than the value. Value must be an integer. |
| `importance.gte` | integer | No | Filter greater than or equal to the value. Value must be an integer. |
| `importance.lt` | integer | No | Filter less than the value. Value must be an integer. |
| `importance.lte` | integer | No | Filter less than or equal to the value. Value must be an integer. |
| `last_updated` | string | No | The timestamp (formatted as an ISO 8601 timestamp) when the rating was last updated in the system. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.gt` | string | No | Filter greater than the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.gte` | string | No | Filter greater than or equal to the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.lt` | string | No | Filter less than the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `last_updated.lte` | string | No | Filter less than or equal to the value. Value must be an integer timestamp in seconds or formatted 'yyyy-mm-dd'. |
| `rating_action` | string | No | The description of the change in rating from the firm's last rating. Possible values include: downgrades, maintains, reinstates, reiterates, upgrades, assumes, initiates_coverage_on, terminates_coverage_on, removes, suspends, firm_dissolved. |
| `rating_action.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `rating_action.gt` | string | No | Filter greater than the value. |
| `rating_action.gte` | string | No | Filter greater than or equal to the value. |
| `rating_action.lt` | string | No | Filter less than the value. |
| `rating_action.lte` | string | No | Filter less than or equal to the value. |
| `price_target_action` | string | No | The description of the directional change in price target. Possible values include: raises, lowers, maintains, announces, sets. |
| `price_target_action.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `price_target_action.gt` | string | No | Filter greater than the value. |
| `price_target_action.gte` | string | No | Filter greater than or equal to the value. |
| `price_target_action.lt` | string | No | Filter less than the value. |
| `price_target_action.lte` | string | No | Filter less than or equal to the value. |
| `benzinga_id` | string | No | The identifer used by Benzinga for this record. |
| `benzinga_id.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `benzinga_id.gt` | string | No | Filter greater than the value. |
| `benzinga_id.gte` | string | No | Filter greater than or equal to the value. |
| `benzinga_id.lt` | string | No | Filter less than the value. |
| `benzinga_id.lte` | string | No | Filter less than or equal to the value. |
| `benzinga_analyst_id` | string | No | The identifer used by Benzinga for this analyst. |
| `benzinga_analyst_id.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `benzinga_analyst_id.gt` | string | No | Filter greater than the value. |
| `benzinga_analyst_id.gte` | string | No | Filter greater than or equal to the value. |
| `benzinga_analyst_id.lt` | string | No | Filter less than the value. |
| `benzinga_analyst_id.lte` | string | No | Filter less than or equal to the value. |
| `benzinga_firm_id` | string | No | The identifer used by Benzinga for this firm. |
| `benzinga_firm_id.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `benzinga_firm_id.gt` | string | No | Filter greater than the value. |
| `benzinga_firm_id.gte` | string | No | Filter greater than or equal to the value. |
| `benzinga_firm_id.lt` | string | No | Filter less than the value. |
| `benzinga_firm_id.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'last_updated' if not specified. The sort order defaults to 'desc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].adjusted_price_target` | number | The current price target adjusted for stock splits and dividends. |
| `results[].analyst` | string | The name of the individual analyst who issued the rating. |
| `results[].benzinga_analyst_id` | string | The identifer used by Benzinga for this analyst. |
| `results[].benzinga_calendar_url` | string | A link to the Benzinga calendar page for this ticker |
| `results[].benzinga_firm_id` | string | The identifer used by Benzinga for this firm. |
| `results[].benzinga_id` | string | The identifer used by Benzinga for this record. |
| `results[].benzinga_news_url` | string | A link to the Benzinga articles page for this ticker |
| `results[].company_name` | string | The name of the company being rated. |
| `results[].currency` | string | The ISO 4217 currency code in which the price target is denominated. |
| `results[].date` | string | The calendar date (formatted as YYYY-MM-DD) when the rating was issued. |
| `results[].firm` | string | The name of the research firm or investment bank issuing the rating. |
| `results[].importance` | integer | A subjective indicator of the importance of the earnings event, on a scale from 0 (lowest) to 5 (highest). |
| `results[].last_updated` | string | The timestamp (formatted as an ISO 8601 timestamp) when the rating was last updated in the system. |
| `results[].notes` | string | Additional context or commentary. |
| `results[].previous_adjusted_price_target` | number | The previous price target adjusted for stock splits and dividends. |
| `results[].previous_price_target` | number | The previous price target set by the analyst. |
| `results[].previous_rating` | string | The previous rating set by the analyst. |
| `results[].price_percent_change` | number | The percentage change in price target if price target and previous price target exists |
| `results[].price_target` | number | The current price target set by the analyst. |
| `results[].price_target_action` | string | The description of the directional change in price target. Possible values include: raises, lowers, maintains, announces, sets. |
| `results[].rating` | string | The current rating set by the analyst. |
| `results[].rating_action` | string | The description of the change in rating from the firm's last rating. Possible values include: downgrades, maintains, reinstates, reiterates, upgrades, assumes, initiates_coverage_on, terminates_coverage_on, removes, suspends, firm_dissolved. |
| `results[].ticker` | string | The stock symbol of the company being rated. |
| `results[].time` | string | The time (formatted as 24-hour HH:MM:SS UTC) when the rating was issued. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "adjusted_price_target": 15,
      "analyst": "Alexander Potter",
      "benzinga_analyst_id": "58933b2043eaaa0001698f4a",
      "benzinga_calendar_url": "https://www.benzinga.com/quote/RIVN/analyst-ratings",
      "benzinga_firm_id": "5e147c6b7da4190001b287b4",
      "benzinga_id": "682f29b0e5343b000100a619",
      "benzinga_news_url": "https://www.benzinga.com/stock-articles/RIVN/analyst-ratings",
      "company_name": "Rivian Automotive",
      "currency": "USD",
      "date": "2025-05-22",
      "firm": "Piper Sandler",
      "importance": 0,
      "last_updated": "2025-05-22T13:42:30Z",
      "previous_adjusted_price_target": 13,
      "previous_price_target": 13,
      "previous_rating": "neutral",
      "price_percent_change": 15.38,
      "price_target": 15,
      "price_target_action": "raises",
      "rating": "neutral",
      "rating_action": "maintains",
      "ticker": "RIVN",
      "time": "09:42:08"
    }
  ],
  "status": "OK"
}
```
