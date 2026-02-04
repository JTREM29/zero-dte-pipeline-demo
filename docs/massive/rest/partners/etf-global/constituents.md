# REST
## Partners

### ETF Constituents

**Endpoint:** `GET /etf-global/v1/constituents`

**Description:**

Access the underlying holdings and constituents of global ETFs. Get detailed information about what securities ETFs hold, providing transparency into fund composition and investment exposure.

Use Cases: ETF portfolio transparency, holdings replication, exposure and risk analysis, fund comparison and benchmarking.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `composite_ticker` | string | No | The stock ticker symbol of the ETF that holds these constituent securities. |
| `composite_ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `composite_ticker.gt` | string | No | Filter greater than the value. |
| `composite_ticker.gte` | string | No | Filter greater than or equal to the value. |
| `composite_ticker.lt` | string | No | Filter less than the value. |
| `composite_ticker.lte` | string | No | Filter less than or equal to the value. |
| `constituent_ticker` | string | No | The stock ticker symbol of the individual security held within the ETF. |
| `constituent_ticker.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `constituent_ticker.gt` | string | No | Filter greater than the value. |
| `constituent_ticker.gte` | string | No | Filter greater than or equal to the value. |
| `constituent_ticker.lt` | string | No | Filter less than the value. |
| `constituent_ticker.lte` | string | No | Filter less than or equal to the value. |
| `effective_date` | string | No | The date showing when the information was accurate or valid; some issuers, such as Vanguard, release their data on a delay, so the effective_date can be several weeks earlier than the processed_date. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `effective_date.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date` | string | No | The date showing when ETF Global received and processed the data. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `processed_date.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `us_code` | string | No | A unique identifier code for the constituent security in US markets. |
| `us_code.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `us_code.gt` | string | No | Filter greater than the value. |
| `us_code.gte` | string | No | Filter greater than or equal to the value. |
| `us_code.lt` | string | No | Filter less than the value. |
| `us_code.lte` | string | No | Filter less than or equal to the value. |
| `isin` | string | No | The International Securities Identification Number, a global standard for identifying securities. |
| `isin.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `isin.gt` | string | No | Filter greater than the value. |
| `isin.gte` | string | No | Filter greater than or equal to the value. |
| `isin.lt` | string | No | Filter less than the value. |
| `isin.lte` | string | No | Filter less than or equal to the value. |
| `figi` | string | No | The Financial Instrument Global Identifier, an open standard for uniquely identifying financial instruments. |
| `figi.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `figi.gt` | string | No | Filter greater than the value. |
| `figi.gte` | string | No | Filter greater than or equal to the value. |
| `figi.lt` | string | No | Filter less than the value. |
| `figi.lte` | string | No | Filter less than or equal to the value. |
| `sedol` | string | No | The Stock Exchange Daily Official List code, primarily used for securities trading in the UK. |
| `sedol.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `sedol.gt` | string | No | Filter greater than the value. |
| `sedol.gte` | string | No | Filter greater than or equal to the value. |
| `sedol.lt` | string | No | Filter less than the value. |
| `sedol.lte` | string | No | Filter less than or equal to the value. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '5000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'composite_ticker' if not specified. The sort order defaults to 'asc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].asset_class` | string | The broad category of asset type, such as Equity, Corporate Bond, Municipal Bond, etc. |
| `results[].composite_ticker` | string | The stock ticker symbol of the ETF that holds these constituent securities. |
| `results[].constituent_name` | string | The full company or security name of the constituent holding. |
| `results[].constituent_rank` | integer | The rank of this constituent within the ETF for a given effective_date, ordered by weight (descending), market_value (descending), and constituent_ticker (ascending). A rank of 1 indicates the largest holding. |
| `results[].constituent_ticker` | string | The stock ticker symbol of the individual security held within the ETF. |
| `results[].country_of_exchange` | string | The country where the exchange that lists this constituent security is located. |
| `results[].currency_traded` | string | The local currency in which this constituent security is denominated and traded. |
| `results[].effective_date` | string | The date showing when the information was accurate or valid; some issuers, such as Vanguard, release their data on a delay, so the effective_date can be several weeks earlier than the processed_date. |
| `results[].exchange` | string | The name of the stock exchange where this constituent security is primarily traded. |
| `results[].figi` | string | The Financial Instrument Global Identifier, an open standard for uniquely identifying financial instruments. |
| `results[].isin` | string | The International Securities Identification Number, a global standard for identifying securities. |
| `results[].market_value` | number | The total market value of this constituent position held by the ETF. |
| `results[].processed_date` | string | The date showing when ETF Global received and processed the data. |
| `results[].security_type` | string | The specific classification of security type using ETF Global's taxonomy, such as Common Equity, Domestic, Global, etc. |
| `results[].sedol` | string | The Stock Exchange Daily Official List code, primarily used for securities trading in the UK. |
| `results[].shares_held` | number | The number of shares of this constituent security that the ETF currently owns. |
| `results[].us_code` | string | A unique identifier code for the constituent security in US markets. |
| `results[].weight` | number | The percentage weight of this constituent security within the ETF's total portfolio. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "asset_class": "Equity",
      "composite_ticker": "SPY",
      "constituent_name": "CAESARS ENTERTAINMENT INC COMMON STOCK",
      "constituent_rank": 42,
      "constituent_ticker": "CZR",
      "country_of_exchange": "US",
      "currency_traded": "USD",
      "effective_date": "2025-09-18",
      "figi": "BBG0074Q3NK6",
      "isin": "US12769G1004",
      "market_value": 63308625.6,
      "processed_date": "2025-09-19",
      "security_type": "Common Stock",
      "sedol": "BMWWGB0",
      "shares_held": 2398054,
      "us_code": "12769G100",
      "weight": 0.0000958005
    }
  ],
  "status": "OK"
}
```
