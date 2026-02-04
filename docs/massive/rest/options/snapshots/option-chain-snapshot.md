# REST
## Options

### Option Chain Snapshot

**Endpoint:** `GET /v3/snapshot/options/{underlyingAsset}`

**Description:**

Retrieve a comprehensive snapshot of all options contracts associated with a specified underlying ticker. This endpoint consolidates key metrics for each contract, including pricing details, greeks (delta, gamma, theta, vega), implied volatility, quotes, trades, and open interest. Users also gain insights into the underlying asset’s current price and break-even calculations. By examining the full options chain in a single request, traders and analysts can evaluate market conditions, compare contract attributes, and refine their strategies.

Use Cases: Market overview, strategy comparison, research and modeling, portfolio refinement.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `underlyingAsset` | string | Yes | The underlying ticker symbol of the option contract. |
| `strike_price` | number | No | Query by strike price of a contract. |
| `expiration_date` | string | No | Query by contract expiration with date format YYYY-MM-DD. |
| `contract_type` | string | No | Query by the type of contract. |
| `strike_price.gte` | number | No | Range by strike_price. |
| `strike_price.gt` | number | No | Range by strike_price. |
| `strike_price.lte` | number | No | Range by strike_price. |
| `strike_price.lt` | number | No | Range by strike_price. |
| `expiration_date.gte` | string | No | Range by expiration_date. |
| `expiration_date.gt` | string | No | Range by expiration_date. |
| `expiration_date.lte` | string | No | Range by expiration_date. |
| `expiration_date.lt` | string | No | Range by expiration_date. |
| `order` | string | No | Order results based on the `sort` field. |
| `limit` | integer | No | Limit the number of results returned, default is 10 and max is 250. |
| `sort` | string | No | Sort field used for ordering. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page of data. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].break_even_price` | number | The price of the underlying asset for the contract to break even. For a call, this value is (strike price + premium paid). For a put, this value is (strike price - premium paid). |
| `results[].day` | object | The most recent daily bar for this contract. |
| `results[].details` | object | The details for this contract. |
| `results[].fmv` | number | Fair Market Value is only available on Business plans. It is our proprietary algorithm to generate a real-time, accurate, fair market value of a tradable security. For more information, <a rel="nofollow" target="_blank" href="https://massive.com/contact">contact us</a>. |
| `results[].fmv_last_updated` | integer | If Fair Market Value (FMV) is available, this field is the nanosecond timestamp of the last FMV calculation. |
| `results[].greeks` | object | The greeks for this contract. There are certain circumstances where greeks will not be returned, such as options contracts that are deep in the money. See this <a href="https://massive.com/blog/greeks-and-implied-volatility/#testing" alt="link">article</a> for more information. |
| `results[].implied_volatility` | number | The market's forecast for the volatility of the underlying asset, based on this option's current price. |
| `results[].last_quote` | object | The most recent quote for this contract. This is only returned if your current plan includes quotes. |
| `results[].last_trade` | object | The most recent trade for this contract. This is only returned if your current plan includes trades. |
| `results[].open_interest` | number | The quantity of this contract held at the end of the last trading day. |
| `results[].underlying_asset` | object | Information on the underlying stock for this options contract.  The market data returned depends on your current stocks plan. |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "request_id": "6a7e466379af0a71039d60cc78e72282",
  "results": [
    {
      "break_even_price": 151.2,
      "day": {
        "change": 4.5,
        "change_percent": 6.76,
        "close": 120.73,
        "high": 120.81,
        "last_updated": 1605195918507251700,
        "low": 118.9,
        "open": 119.32,
        "previous_close": 119.12,
        "volume": 868,
        "vwap": 119.31
      },
      "details": {
        "contract_type": "call",
        "exercise_style": "american",
        "expiration_date": "2022-01-21",
        "shares_per_contract": 100,
        "strike_price": 150,
        "ticker": "O:AAPL211022C000150000"
      },
      "fmv": 0.05,
      "fmv_last_updated": 1605195918508251600,
      "greeks": {
        "delta": 1,
        "gamma": 0,
        "theta": 0.00229,
        "vega": 0
      },
      "implied_volatility": 5,
      "last_quote": {
        "ask": 120.3,
        "ask_size": 4,
        "bid": 120.28,
        "bid_size": 8,
        "last_updated": 1605195918507251700,
        "midpoint": 120.29,
        "timeframe": "REAL-TIME"
      },
      "last_trade": {
        "conditions": [
          209
        ],
        "exchange": 316,
        "price": 0.05,
        "sip_timestamp": 1675280958783136800,
        "size": 2,
        "timeframe": "REAL-TIME"
      },
      "open_interest": 1543,
      "underlying_asset": {
        "change_to_break_even": 4.2,
        "last_updated": 1605195918507251700,
        "price": 147,
        "ticker": "AAPL",
        "timeframe": "DELAYED"
      }
    }
  ],
  "status": "OK"
}
```
