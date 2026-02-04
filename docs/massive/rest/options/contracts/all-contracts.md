# REST
## Options

### All Contracts

**Endpoint:** `GET /v3/reference/options/contracts`

**Description:**

Retrieve a comprehensive index of options contracts, encompassing both active and expired listings. This endpoint can return a broad selection of contracts or be narrowed down to those tied to a specific underlying ticker. Each contract entry includes details such as contract type (call/put), exercise style, expiration date, and strike price. By exploring this index, users can assess market availability, analyze contract characteristics, and refine their options trading or research strategies.

Use Cases: Market availability analysis, strategy development, research and modeling, contract exploration.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `underlying_ticker` | string | No | Query for contracts relating to an underlying stock ticker. |
| `ticker` | string | No | This parameter has been deprecated. To search by specific options ticker, use the Options Contract endpoint [here](https://massive.com/docs/rest/options/contracts/contract-overview). |
| `contract_type` | string | No | Query by the type of contract. |
| `expiration_date` | string | No | Query by contract expiration with date format YYYY-MM-DD. |
| `as_of` | string | No | Specify a point in time for contracts as of this date with format YYYY-MM-DD. Defaults to today's date. |
| `strike_price` | number | No | Query by strike price of a contract. |
| `expired` | boolean | No | Query for expired contracts. Default is false. |
| `underlying_ticker.gte` | string | No | Range by underlying_ticker. |
| `underlying_ticker.gt` | string | No | Range by underlying_ticker. |
| `underlying_ticker.lte` | string | No | Range by underlying_ticker. |
| `underlying_ticker.lt` | string | No | Range by underlying_ticker. |
| `expiration_date.gte` | string | No | Range by expiration_date. |
| `expiration_date.gt` | string | No | Range by expiration_date. |
| `expiration_date.lte` | string | No | Range by expiration_date. |
| `expiration_date.lt` | string | No | Range by expiration_date. |
| `strike_price.gte` | number | No | Range by strike_price. |
| `strike_price.gt` | number | No | Range by strike_price. |
| `strike_price.lte` | number | No | Range by strike_price. |
| `strike_price.lt` | number | No | Range by strike_price. |
| `order` | string | No | Order results based on the `sort` field. |
| `limit` | integer | No | Limit the number of results returned, default is 10 and max is 1000. |
| `sort` | string | No | Sort field used for ordering. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page of data. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].additional_underlyings` | array[object] | If an option contract has additional underlyings or deliverables associated with it, they will appear here. See <a rel="noopener noreferrer nofollow" target="_blank" href="https://www.optionseducation.org/referencelibrary/faq/splits-mergers-spinoffs-bankruptcies">here</a> for some examples of what might cause a contract to have additional underlyings. |
| `results[].cfi` | string | The 6 letter CFI code of the contract (defined in <a rel="nofollow" target="_blank" href="https://en.wikipedia.org/wiki/ISO_10962">ISO 10962</a>) |
| `results[].contract_type` | string | The type of contract. Can be "put", "call", or in some rare cases, "other". |
| `results[].correction` | integer | The correction number for this option contract. |
| `results[].exercise_style` | enum: american, european, bermudan | The exercise style of this contract. See <a rel="nofollow" target="_blank" href="https://en.wikipedia.org/wiki/Option_style">this link</a> for more details on exercise styles. |
| `results[].expiration_date` | string | The contract's expiration date in YYYY-MM-DD format. |
| `results[].primary_exchange` | string | The MIC code of the primary exchange that this contract is listed on. |
| `results[].shares_per_contract` | number | The number of shares per contract for this contract. |
| `results[].strike_price` | number | The strike price of the option contract. |
| `results[].ticker` | string | The ticker for the option contract. |
| `results[].underlying_ticker` | string | The underlying ticker that the option contract relates to. |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "request_id": "603902c0-a5a5-406f-bd08-f030f92418fa",
  "results": [
    {
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
    {
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
      "strike_price": 90,
      "ticker": "O:AAPL211119C00090000",
      "underlying_ticker": "AAPL"
    }
  ],
  "status": "OK"
}
```
