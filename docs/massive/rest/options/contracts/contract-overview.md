# REST
## Options

### Contract Overview

**Endpoint:** `GET /v3/reference/options/contracts/{options_ticker}`

**Description:**

Retrieve detailed information about a specific options contract, including its contract type (call or put), exercise style, expiration date, strike price, shares per contract, underlying ticker, and primary exchange. This endpoint provides essential attributes for understanding the contract’s structure and evaluating it within broader options strategies and portfolios.

Use Cases: Contract specifications reference, option chain analysis, strategy development, portfolio integration.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `options_ticker` | string | Yes | Query for a contract by options ticker. You can learn more about the structure of options tickers [here](https://massive.com/blog/how-to-read-a-stock-options-ticker/). |
| `as_of` | string | No | Specify a point in time for the contract as of this date with format YYYY-MM-DD. Defaults to today's date. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `request_id` | string | A request id assigned by the server. |
| `results` | object | Contains the requested data for the specified options contract. |
| `results.additional_underlyings` | array[object] | If an option contract has additional underlyings or deliverables associated with it, they will appear here. See <a rel="noopener noreferrer nofollow" target="_blank" href="https://www.optionseducation.org/referencelibrary/faq/splits-mergers-spinoffs-bankruptcies">here</a> for some examples of what might cause a contract to have additional underlyings. |
| `results.cfi` | string | The 6 letter CFI code of the contract (defined in <a rel="nofollow" target="_blank" href="https://en.wikipedia.org/wiki/ISO_10962">ISO 10962</a>) |
| `results.contract_type` | string | The type of contract. Can be "put", "call", or in some rare cases, "other". |
| `results.correction` | integer | The correction number for this option contract. |
| `results.exercise_style` | enum: american, european, bermudan | The exercise style of this contract. See <a rel="nofollow" target="_blank" href="https://en.wikipedia.org/wiki/Option_style">this link</a> for more details on exercise styles. |
| `results.expiration_date` | string | The contract's expiration date in YYYY-MM-DD format. |
| `results.primary_exchange` | string | The MIC code of the primary exchange that this contract is listed on. |
| `results.shares_per_contract` | number | The number of shares per contract for this contract. |
| `results.strike_price` | number | The strike price of the option contract. |
| `results.ticker` | string | The ticker for the option contract. |
| `results.underlying_ticker` | string | The underlying ticker that the option contract relates to. |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "request_id": "603902c0-a5a5-406f-bd08-f030f92418fa",
  "results": {
    "additional_underlyings": [
      {
        "amount": 44,
        "type": "equity",
        "underlying": "VMW"
      },
      {
        "amount": 6.53,
        "type": "currency",
        "underlying": "USD"
      }
    ],
    "cfi": "OCASPS",
    "contract_type": "call",
    "exercise_style": "american",
    "expiration_date": "2021-11-19",
    "primary_exchange": "BATO",
    "shares_per_contract": 100,
    "strike_price": 85,
    "ticker": "O:AAPL211119C00085000",
    "underlying_ticker": "AAPL"
  },
  "status": "OK"
}
```
