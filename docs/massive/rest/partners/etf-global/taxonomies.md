# REST
## Partners

### ETF Taxonomies

**Endpoint:** `GET /etf-global/v1/taxonomies`

**Description:**

Access standardized taxonomy systems used to categorize and organize global ETFs. Get structured classification frameworks that help define investment strategies, asset types, and fund characteristics.

Use Cases: ETF classification and categorization, strategy identification, taxonomy-based screening, fund universe organization.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `processed_date` | string | No | The date showing when ETF Global received and processed the data. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date` | string | No | The date showing when the information was accurate or valid; some issuers, such as Vanguard, release their data on a delay, so the effective_date can be several weeks earlier than the processed_date. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `composite_ticker` | string | No | The stock ticker symbol used to identify this ETF product on exchanges. |
| `composite_ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `composite_ticker.gt` | string | No | Filter greater than the value. |
| `composite_ticker.gte` | string | No | Filter greater than or equal to the value. |
| `composite_ticker.lt` | string | No | Filter less than the value. |
| `composite_ticker.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '5000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'composite_ticker' if not specified. The sort order defaults to 'asc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].asset_class` | string | The primary type of assets held by the ETF, such as equities, bonds, commodities, or other securities. |
| `results[].category` | string | The broad investment category that describes the ETF's investment focus and strategy. |
| `results[].composite_ticker` | string | The stock ticker symbol used to identify this ETF product on exchanges. |
| `results[].country` | string | The specific country focus of the ETF, if applicable. |
| `results[].credit_quality_rating` | string | Credit quality rating for fixed income ETFs. |
| `results[].description` | string | The official name and description of the ETF product. |
| `results[].development_class` | string | The economic development classification of the markets the ETF invests in, such as developed, emerging, or frontier markets. |
| `results[].duration` | string | The duration characteristics for fixed income ETFs. |
| `results[].effective_date` | string | The date showing when the information was accurate or valid; some issuers, such as Vanguard, release their data on a delay, so the effective_date can be several weeks earlier than the processed_date. |
| `results[].esg` | string | Environmental, Social, and Governance characteristics. |
| `results[].exposure_mechanism` | string | The mechanism used to achieve exposure. |
| `results[].factor` | string | Factor exposure characteristics of the ETF. |
| `results[].focus` | string | The specific investment focus or exposure that the ETF provides, such as sector, geography, or investment style. |
| `results[].hedge_reset` | string | The frequency of hedge reset, if applicable. |
| `results[].holdings_disclosure_frequency` | string | How frequently holdings are disclosed. |
| `results[].inception_date` | string | The date when this ETF was first launched and became available for trading. |
| `results[].isin` | string | The International Securities Identification Number, a global standard code for uniquely identifying this ETF worldwide. |
| `results[].issuer` | string | The financial institution or fund company that created and sponsors this ETF. |
| `results[].leverage_reset` | string | The frequency of leverage reset, if applicable. |
| `results[].leverage_style` | string | Indicates whether the ETF uses leverage to amplify returns ('leveraged'), or does not use leverage ('unleveraged'). |
| `results[].levered_amount` | number | The leverage multiplier applied by the ETF, where positive numbers indicate leveraged exposure and negative numbers indicate inverse exposure. |
| `results[].management_classification` | string | Defines whether an ETF is considered active under SEC rules, with managers making investment decisions, or passive, tracking an index. |
| `results[].management_style` | string | Indicates whether an ETF is managed actively or passively, and the level of transparency or replication method used. |
| `results[].maturity` | string | The maturity profile for fixed income ETFs. |
| `results[].objective` | string | The primary investment objective of the ETF. |
| `results[].primary_benchmark` | string | The main index or benchmark that this ETF is designed to track or replicate. |
| `results[].processed_date` | string | The date showing when ETF Global received and processed the data. |
| `results[].product_type` | string | Indicates whether the product is an Exchange-Traded Note ('etn') or an Exchange-Traded Fund ('etf'). |
| `results[].rebalance_frequency` | string | How frequently the ETF rebalances its holdings. |
| `results[].reconstitution_frequency` | string | How frequently the index is reconstituted. |
| `results[].region` | string | The geographic region or area of the world where the ETF concentrates its investments. |
| `results[].secondary_objective` | string | The secondary investment objective, if applicable. |
| `results[].selection_methodology` | string | The methodology used to select securities. |
| `results[].selection_universe` | string | The universe from which securities are selected. |
| `results[].strategic_focus` | string | The strategic investment focus of the ETF. |
| `results[].targeted_focus` | string | The targeted investment focus of the ETF. |
| `results[].tax_classification` | string | The tax structure of the ETF, determining whether investors receive 1099 or K1 tax forms (RIC, Partnership, or UIT). |
| `results[].us_code` | string | A unique identifier code that identifies this ETF in US markets. |
| `results[].weighting_methodology` | string | The methodology used to weight holdings. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "asset_class": "Equity",
      "category": "Size and Style",
      "composite_ticker": "SPY",
      "country": "U.S.",
      "description": "SPDR S&P 500 ETF Trust",
      "development_class": "Developed Markets",
      "effective_date": "2025-09-19",
      "exposure_mechanism": "Blended Replication",
      "factor": "Size",
      "focus": "Large Cap",
      "holdings_disclosure_frequency": "Daily",
      "inception_date": "1993-01-22",
      "isin": "US78462F1030",
      "issuer": "SSgA",
      "leverage_style": "unleveraged",
      "levered_amount": 0,
      "management_classification": "passive",
      "management_style": "Passive - Representative Sampling",
      "objective": "Index-Tracking",
      "primary_benchmark": "S&P 500 Index",
      "processed_date": "2025-09-19",
      "product_type": "etf",
      "rebalance_frequency": "Quarterly",
      "reconstitution_frequency": "Quarterly",
      "region": "North America",
      "selection_methodology": "Modified Market Cap, Fundamental Multifactor, Liquidity",
      "selection_universe": "U.S. Large Caps",
      "strategic_focus": "Factor",
      "targeted_focus": "Size",
      "tax_classification": "Regulated Investment Company",
      "us_code": "78462F103",
      "weighting_methodology": "Modified Market Capitalization-Weighted"
    }
  ],
  "status": "OK"
}
```
