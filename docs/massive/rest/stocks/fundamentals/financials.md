# REST
## Stocks

### Financials (Deprecated)

**Endpoint:** `GET /vX/reference/financials`

**Description:**

Retrieve historical financial data for a specified stock ticker, derived from company SEC filings and extracted via XBRL. This experimental endpoint provides a wide range of financial metrics, including income statements, balance sheets, cash flow statements, and comprehensive income figures. By examining these standardized, machine-readable financial details, users can conduct in-depth fundamental analysis, track corporate performance trends, and compare financials across different reporting periods.

Use Cases: Fundamental analysis, trend identification, cross-company comparisons, research & modeling.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | No | Query by company ticker. |
| `cik` | string | No | Query by central index key (<a rel="noopener noreferrer nofollow" target="_blank" href="https://www.sec.gov/edgar/searchedgar/cik.htm">CIK</a>) Number |
| `company_name` | string | No | Query by company name. |
| `sic` | string | No | Query by standard industrial classification (<a rel="noopener noreferrer nofollow" target="_blank" href="https://www.sec.gov/corpfin/division-of-corporation-finance-standard-industrial-classification-sic-code-list">SIC</a>) |
| `filing_date` | string | No | Query by the date when the filing with financials data was filed in YYYY-MM-DD format.  Best used when querying over date ranges to find financials based on filings that happen in a time period.  Examples:  To get financials based on filings that have happened after January 1, 2009 use the query param filing_date.gte=2009-01-01  To get financials based on filings that happened in the year 2009 use the query params filing_date.gte=2009-01-01&filing_date.lt=2010-01-01 |
| `period_of_report_date` | string | No | The period of report for the filing with financials data in YYYY-MM-DD format. |
| `timeframe` | string | No | Query by timeframe. Annual financials originate from 10-K filings, and quarterly financials originate from 10-Q filings. Note: Most companies do not file quarterly reports for Q4 and instead include those financials in their annual report, so some companies my not return quarterly financials for Q4 |
| `include_sources` | boolean | No | Whether or not to include the `xpath` and `formula` attributes for each financial data point. See the `xpath` and `formula` response attributes for more info. False by default. |
| `company_name.search` | string | No | Search by company_name. |
| `filing_date.gte` | string | No | Search by filing_date. |
| `filing_date.gt` | string | No | Search by filing_date. |
| `filing_date.lte` | string | No | Search by filing_date. |
| `filing_date.lt` | string | No | Search by filing_date. |
| `period_of_report_date.gte` | string | No | Search by period_of_report_date. |
| `period_of_report_date.gt` | string | No | Search by period_of_report_date. |
| `period_of_report_date.lte` | string | No | Search by period_of_report_date. |
| `period_of_report_date.lt` | string | No | Search by period_of_report_date. |
| `order` | string | No | Order results based on the `sort` field. |
| `limit` | integer | No | Limit the number of results returned, default is 10 and max is 100. |
| `sort` | string | No | Sort field used for ordering. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `count` | integer | The total number of results for this request. |
| `next_url` | string | If present, this value can be used to fetch the next page of data. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].acceptance_datetime` | string | The datetime (EST timezone) the filing was accepted by EDGAR in YYYYMMDDHHMMSS format. |
| `results[].cik` | string | The CIK number for the company. |
| `results[].company_name` | string | The company name. |
| `results[].end_date` | string | The end date of the period that these financials cover in YYYYMMDD format. |
| `results[].filing_date` | string | The date that the SEC filing which these financials were derived from was made available. Note that this is not necessarily the date when this information became public, as some companies may publish a press release before filing with the SEC. |
| `results[].financials` | object | Structured financial statements with detailed data points and metadata. |
| `results[].fiscal_period` | string | Fiscal period of the report according to the company (Q1, Q2, Q3, Q4, or FY). |
| `results[].fiscal_year` | string | Fiscal year of the report according to the company. |
| `results[].sic` | string | The Standard Industrial Classification (SIC) code for the company. |
| `results[].source_filing_file_url` | string | The URL of the specific XBRL instance document within the SEC filing that these financials were derived from. |
| `results[].source_filing_url` | string | The URL of the SEC filing that these financials were derived from. |
| `results[].start_date` | string | The start date of the period that these financials cover in YYYYMMDD format. |
| `results[].tickers` | array[string] | The list of ticker symbols for the company. |
| `results[].timeframe` | string | The timeframe of the report (quarterly, annual or ttm). |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "next_url": "https://api.massive.com/vX/reference/financials?",
  "request_id": "55eb92ed43b25568ab0cce159830ea34",
  "results": [
    {
      "cik": "0001650729",
      "company_name": "SiteOne Landscape Supply, Inc.",
      "end_date": "2022-04-03",
      "filing_date": "2022-05-04",
      "financials": {
        "balance_sheet": {
          "assets": {
            "label": "Assets",
            "order": 100,
            "unit": "USD",
            "value": 2407400000
          },
          "current_assets": {
            "label": "Current Assets",
            "order": 200,
            "unit": "USD",
            "value": 1385900000
          },
          "current_liabilities": {
            "label": "Current Liabilities",
            "order": 700,
            "unit": "USD",
            "value": 597500000
          },
          "equity": {
            "label": "Equity",
            "order": 1400,
            "unit": "USD",
            "value": 1099200000
          },
          "equity_attributable_to_noncontrolling_interest": {
            "label": "Equity Attributable To Noncontrolling Interest",
            "order": 1500,
            "unit": "USD",
            "value": 0
          },
          "equity_attributable_to_parent": {
            "label": "Equity Attributable To Parent",
            "order": 1600,
            "unit": "USD",
            "value": 1099200000
          },
          "liabilities": {
            "label": "Liabilities",
            "order": 600,
            "unit": "USD",
            "value": 1308200000
          },
          "liabilities_and_equity": {
            "label": "Liabilities And Equity",
            "order": 1900,
            "unit": "USD",
            "value": 2407400000
          },
          "noncurrent_assets": {
            "label": "Noncurrent Assets",
            "order": 300,
            "unit": "USD",
            "value": 1021500000
          },
          "noncurrent_liabilities": {
            "label": "Noncurrent Liabilities",
            "order": 800,
            "unit": "USD",
            "value": 710700000
          }
        },
        "cash_flow_statement": {
          "exchange_gains_losses": {
            "label": "Exchange Gains/Losses",
            "order": 1000,
            "unit": "USD",
            "value": 100000
          },
          "net_cash_flow": {
            "label": "Net Cash Flow",
            "order": 1100,
            "unit": "USD",
            "value": -8600000
          },
          "net_cash_flow_continuing": {
            "label": "Net Cash Flow, Continuing",
            "order": 1200,
            "unit": "USD",
            "value": -8700000
          },
          "net_cash_flow_from_financing_activities": {
            "label": "Net Cash Flow From Financing Activities",
            "order": 700,
            "unit": "USD",
            "value": 150600000
          },
          "net_cash_flow_from_financing_activities_continuing": {
            "label": "Net Cash Flow From Financing Activities, Continuing",
            "order": 800,
            "unit": "USD",
            "value": 150600000
          },
          "net_cash_flow_from_investing_activities": {
            "label": "Net Cash Flow From Investing Activities",
            "order": 400,
            "unit": "USD",
            "value": -41000000
          },
          "net_cash_flow_from_investing_activities_continuing": {
            "label": "Net Cash Flow From Investing Activities, Continuing",
            "order": 500,
            "unit": "USD",
            "value": -41000000
          },
          "net_cash_flow_from_operating_activities": {
            "label": "Net Cash Flow From Operating Activities",
            "order": 100,
            "unit": "USD",
            "value": -118300000
          },
          "net_cash_flow_from_operating_activities_continuing": {
            "label": "Net Cash Flow From Operating Activities, Continuing",
            "order": 200,
            "unit": "USD",
            "value": -118300000
          }
        },
        "comprehensive_income": {
          "comprehensive_income_loss": {
            "label": "Comprehensive Income/Loss",
            "order": 100,
            "unit": "USD",
            "value": 40500000
          },
          "comprehensive_income_loss_attributable_to_noncontrolling_interest": {
            "label": "Comprehensive Income/Loss Attributable To Noncontrolling Interest",
            "order": 200,
            "unit": "USD",
            "value": 0
          },
          "comprehensive_income_loss_attributable_to_parent": {
            "label": "Comprehensive Income/Loss Attributable To Parent",
            "order": 300,
            "unit": "USD",
            "value": 40500000
          },
          "other_comprehensive_income_loss": {
            "label": "Other Comprehensive Income/Loss",
            "order": 400,
            "unit": "USD",
            "value": 40500000
          },
          "other_comprehensive_income_loss_attributable_to_parent": {
            "label": "Other Comprehensive Income/Loss Attributable To Parent",
            "order": 600,
            "unit": "USD",
            "value": 8200000
          }
        },
        "income_statement": {
          "basic_earnings_per_share": {
            "label": "Basic Earnings Per Share",
            "order": 4200,
            "unit": "USD / shares",
            "value": 0.72
          },
          "benefits_costs_expenses": {
            "label": "Benefits Costs and Expenses",
            "order": 200,
            "unit": "USD",
            "value": 768400000
          },
          "cost_of_revenue": {
            "label": "Cost Of Revenue",
            "order": 300,
            "unit": "USD",
            "value": 536100000
          },
          "costs_and_expenses": {
            "label": "Costs And Expenses",
            "order": 600,
            "unit": "USD",
            "value": 768400000
          },
          "diluted_earnings_per_share": {
            "label": "Diluted Earnings Per Share",
            "order": 4300,
            "unit": "USD / shares",
            "value": 0.7
          },
          "gross_profit": {
            "label": "Gross Profit",
            "order": 800,
            "unit": "USD",
            "value": 269200000
          },
          "income_loss_from_continuing_operations_after_tax": {
            "label": "Income/Loss From Continuing Operations After Tax",
            "order": 1400,
            "unit": "USD",
            "value": 32300000
          },
          "income_loss_from_continuing_operations_before_tax": {
            "label": "Income/Loss From Continuing Operations Before Tax",
            "order": 1500,
            "unit": "USD",
            "value": 36900000
          },
          "income_tax_expense_benefit": {
            "label": "Income Tax Expense/Benefit",
            "order": 2200,
            "unit": "USD",
            "value": 4600000
          },
          "interest_expense_operating": {
            "label": "Interest Expense, Operating",
            "order": 2700,
            "unit": "USD",
            "value": 4300000
          },
          "net_income_loss": {
            "label": "Net Income/Loss",
            "order": 3200,
            "unit": "USD",
            "value": 32300000
          },
          "net_income_loss_attributable_to_noncontrolling_interest": {
            "label": "Net Income/Loss Attributable To Noncontrolling Interest",
            "order": 3300,
            "unit": "USD",
            "value": 0
          },
          "net_income_loss_attributable_to_parent": {
            "label": "Net Income/Loss Attributable To Parent",
            "order": 3500,
            "unit": "USD",
            "value": 32300000
          },
          "net_income_loss_available_to_common_stockholders_basic": {
            "label": "Net Income/Loss Available To Common Stockholders, Basic",
            "order": 3700,
            "unit": "USD",
            "value": 32300000
          },
          "operating_expenses": {
            "label": "Operating Expenses",
            "order": 1000,
            "unit": "USD",
            "value": 228000000
          },
          "operating_income_loss": {
            "label": "Operating Income/Loss",
            "order": 1100,
            "unit": "USD",
            "value": 41200000
          },
          "participating_securities_distributed_and_undistributed_earnings_loss_basic": {
            "label": "Participating Securities, Distributed And Undistributed Earnings/Loss, Basic",
            "order": 3800,
            "unit": "USD",
            "value": 0
          },
          "preferred_stock_dividends_and_other_adjustments": {
            "label": "Preferred Stock Dividends And Other Adjustments",
            "order": 3900,
            "unit": "USD",
            "value": 0
          },
          "revenues": {
            "label": "Revenues",
            "order": 100,
            "unit": "USD",
            "value": 805300000
          }
        }
      },
      "fiscal_period": "Q1",
      "fiscal_year": "2022",
      "source_filing_file_url": "https://api.massive.com/v1/reference/sec/filings/0001650729-22-000010/files/site-20220403_htm.xml",
      "source_filing_url": "https://api.massive.com/v1/reference/sec/filings/0001650729-22-000010",
      "start_date": "2022-01-03"
    }
  ],
  "status": "OK"
}
```
