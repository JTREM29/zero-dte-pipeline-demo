# REST
## Stocks

### 10-K Sections

**Endpoint:** `GET /stocks/filings/10-K/vX/sections`

**Description:**

Plain-text content of specific sections from SEC filings. Currently supports the Risk Factors and Business sections, providing clean, structured text extracted directly from the filing.

Use Cases: NLP pipelines, text extraction, research automation, topic analysis, building summaries or similarity models.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `cik` | string | No | SEC Central Index Key (10 digits, zero-padded). |
| `cik.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `cik.gt` | string | No | Filter greater than the value. |
| `cik.gte` | string | No | Filter greater than or equal to the value. |
| `cik.lt` | string | No | Filter less than the value. |
| `cik.lte` | string | No | Filter less than or equal to the value. |
| `ticker` | string | No | Stock ticker symbol for the company. |
| `ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `ticker.gt` | string | No | Filter greater than the value. |
| `ticker.gte` | string | No | Filter greater than or equal to the value. |
| `ticker.lt` | string | No | Filter less than the value. |
| `ticker.lte` | string | No | Filter less than or equal to the value. |
| `section` | string | No | Standardized section identifier from the filing (e.g. 'business', 'risk_factors', etc.). |
| `section.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `filing_date` | string | No | Date when the filing was submitted to the SEC (formatted as YYYY-MM-DD). Value must be formatted 'yyyy-mm-dd'. |
| `filing_date.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `filing_date.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `filing_date.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `filing_date.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `period_end` | string | No | Period end date that the filing relates to (formatted as YYYY-MM-DD). Value must be formatted 'yyyy-mm-dd'. |
| `period_end.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `period_end.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `period_end.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `period_end.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '9999'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'period_end' if not specified. The sort order defaults to 'desc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].cik` | string | SEC Central Index Key (10 digits, zero-padded). |
| `results[].filing_date` | string | Date when the filing was submitted to the SEC (formatted as YYYY-MM-DD). |
| `results[].filing_url` | string | SEC URL source for the full filing. |
| `results[].period_end` | string | Period end date that the filing relates to (formatted as YYYY-MM-DD). |
| `results[].section` | string | Standardized section identifier from the filing (e.g. 'business', 'risk_factors', etc.). |
| `results[].text` | string | Full raw text content of the section, including headers and formatting. |
| `results[].ticker` | string | Stock ticker symbol for the company. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 2,
  "next_url": "https://api.massive.com/stocks/filings/10-K/vX/sections?cursor=eyJsaW1pd...",
  "request_id": "a3f8b2c1d4e5f6g7",
  "results": [
    {
      "cik": "0000320193",
      "filing_date": "2023-11-03",
      "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/0000320193-23-000106.txt",
      "period_end": "2023-09-30",
      "section": "risk_factors",
      "text": "Item 1A. Risk Factors\n\nInvesting in our stock involves risk. In addition to the other information in this Annual Report on Form 10-K, the following risk factors should be carefully considered...",
      "ticker": "AAPL"
    },
    {
      "cik": "0000789019",
      "filing_date": "2023-07-27",
      "filing_url": "https://www.sec.gov/Archives/edgar/data/789019/0000950170-23-035122.txt",
      "period_end": "2023-06-30",
      "section": "risk_factors",
      "text": "Item 1A. RISK FACTORS\n\nOur operations and financial results are subject to various risks and uncertainties...",
      "ticker": "MSFT"
    }
  ],
  "status": "OK"
}
```
