# REST
## Partners

### Corporate Events

**Endpoint:** `GET /tmx/v1/corporate-events`

**Description:**

Retrieve structured corporate event data from Wall Street Horizon's comprehensive global events calendar, including earnings announcements, dividend dates, investor conferences, and stock splits. Each event record includes essential attributes such as event type, scheduled date, event status (e.g., confirmed, pending, canceled), ISIN, ticker, and direct links to primary announcement sources when available. Data can be filtered (by ticker, event type, etc.), and records are timestamped for seamless integration into trading and analysis workflows.

Use Cases: Financial event tracking, market sentiment analysis, trading strategy formulation, risk management, corporate actions management.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `date` | string | No | Scheduled date of the corporate event, formatted as YYYY-MM-DD. |
| `date.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `date.gt` | string | No | Filter greater than the value. |
| `date.gte` | string | No | Filter greater than or equal to the value. |
| `date.lt` | string | No | Filter less than the value. |
| `date.lte` | string | No | Filter less than or equal to the value. |
| `type` | string | No | The normalized type of corporate event. Possible values include: analyst_day, business_update, capital_markets_day, conference, dividend, earnings_announcement_date, earnings_conference_call, earnings_results_announcement, forum, interim_statement, other_interim_announcement, production_update, research_and_development_day, seminar, shareholder_meeting, sales_update, stock_split, summit, service_level_update, tradeshow, company_travel, and workshop. |
| `type.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `type.gt` | string | No | Filter greater than the value. |
| `type.gte` | string | No | Filter greater than or equal to the value. |
| `type.lt` | string | No | Filter less than the value. |
| `type.lte` | string | No | Filter less than or equal to the value. |
| `status` | string | No | The current status of the event. Possible values include: approved, canceled, confirmed, historical, pending_approval, postponed, and unconfirmed. |
| `status.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `status.gt` | string | No | Filter greater than the value. |
| `status.gte` | string | No | Filter greater than or equal to the value. |
| `status.lt` | string | No | Filter less than the value. |
| `status.lte` | string | No | Filter less than or equal to the value. |
| `ticker` | string | No | The company's stock symbol. |
| `ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `ticker.gt` | string | No | Filter greater than the value. |
| `ticker.gte` | string | No | Filter greater than or equal to the value. |
| `ticker.lt` | string | No | Filter less than the value. |
| `ticker.lte` | string | No | Filter less than or equal to the value. |
| `isin` | string | No | Standard international identifier for the company's common stock. |
| `isin.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `isin.gt` | string | No | Filter greater than the value. |
| `isin.gte` | string | No | Filter greater than or equal to the value. |
| `isin.lt` | string | No | Filter less than the value. |
| `isin.lte` | string | No | Filter less than or equal to the value. |
| `trading_venue` | string | No | MIC (Market Identifier Code) of the exchange where the company's stock is listed. |
| `trading_venue.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `trading_venue.gt` | string | No | Filter greater than the value. |
| `trading_venue.gte` | string | No | Filter greater than or equal to the value. |
| `trading_venue.lt` | string | No | Filter less than the value. |
| `trading_venue.lte` | string | No | Filter less than or equal to the value. |
| `tmx_company_id` | integer | No | Unique numeric identifier for the company used by TMX. Value must be an integer. |
| `tmx_company_id.gt` | integer | No | Filter greater than the value. Value must be an integer. |
| `tmx_company_id.gte` | integer | No | Filter greater than or equal to the value. Value must be an integer. |
| `tmx_company_id.lt` | integer | No | Filter less than the value. Value must be an integer. |
| `tmx_company_id.lte` | integer | No | Filter less than or equal to the value. Value must be an integer. |
| `tmx_record_id` | string | No | The unique alphanumeric identifier for the event record used by TMX. |
| `tmx_record_id.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `tmx_record_id.gt` | string | No | Filter greater than the value. |
| `tmx_record_id.gte` | string | No | Filter greater than or equal to the value. |
| `tmx_record_id.lt` | string | No | Filter less than the value. |
| `tmx_record_id.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'date' if not specified. The sort order defaults to 'desc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].company_name` | string | Full name of the company. |
| `results[].date` | string | Scheduled date of the corporate event, formatted as YYYY-MM-DD. |
| `results[].isin` | string | Standard international identifier for the company's common stock. |
| `results[].name` | string | Name or title of the event. |
| `results[].status` | string | The current status of the event. Possible values include: approved, canceled, confirmed, historical, pending_approval, postponed, and unconfirmed. |
| `results[].ticker` | string | The company's stock symbol. |
| `results[].tmx_company_id` | integer | Unique numeric identifier for the company used by TMX. |
| `results[].tmx_record_id` | string | The unique alphanumeric identifier for the event record used by TMX. |
| `results[].trading_venue` | string | MIC (Market Identifier Code) of the exchange where the company's stock is listed. |
| `results[].type` | string | The normalized type of corporate event. Possible values include: analyst_day, business_update, capital_markets_day, conference, dividend, earnings_announcement_date, earnings_conference_call, earnings_results_announcement, forum, interim_statement, other_interim_announcement, production_update, research_and_development_day, seminar, shareholder_meeting, sales_update, stock_split, summit, service_level_update, tradeshow, company_travel, and workshop. |
| `results[].url` | string | URL linking to the primary public source of the event announcement, if available. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "company_name": "Rollins Inc.",
      "date": "2025-07-23",
      "isin": "US7757111049",
      "name": "Q2 2025 Earnings Announcement-After Mkt",
      "status": "unconfirmed",
      "ticker": "ROL",
      "tmx_company_id": "2208",
      "tmx_record_id": "4XMW4E9G",
      "trading_venue": "XNYS",
      "type": "earnings_announcement_date"
    }
  ],
  "status": "OK"
}
```
