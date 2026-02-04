# REST
## Stocks

### Dividends (Deprecated)

**Endpoint:** `GET /v3/reference/dividends`

**Description:**

Retrieve a historical record of cash dividend distributions for a given ticker, including declaration, ex-dividend, record, and pay dates, as well as payout amounts and frequency. This endpoint consolidates key dividend information, enabling users to account for dividend income in returns, develop dividend-focused strategies, and support tax reporting needs.

Use Cases: Income analysis, total return calculations, dividend strategies, tax planning.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker` | string | No | Specify a case-sensitive ticker symbol. For example, AAPL represents Apple Inc. |
| `ex_dividend_date` | string | No | Query by ex-dividend date with the format YYYY-MM-DD. |
| `record_date` | string | No | Query by record date with the format YYYY-MM-DD. |
| `declaration_date` | string | No | Query by declaration date with the format YYYY-MM-DD. |
| `pay_date` | string | No | Query by pay date with the format YYYY-MM-DD. |
| `frequency` | integer | No | Query by the number of times per year the dividend is paid out.  Possible values are 0 (one-time), 1 (annually), 2 (bi-annually), 4 (quarterly), 12 (monthly), 24 (bi-monthly), and 52 (weekly). |
| `cash_amount` | number | No | Query by the cash amount of the dividend. |
| `dividend_type` | string | No | Query by the type of dividend. Dividends that have been paid and/or are expected to be paid on consistent schedules are denoted as CD. Special Cash dividends that have been paid that are infrequent or unusual, and/or can not be expected to occur in the future are denoted as SC. |
| `ticker.gte` | string | No | Range by ticker. |
| `ticker.gt` | string | No | Range by ticker. |
| `ticker.lte` | string | No | Range by ticker. |
| `ticker.lt` | string | No | Range by ticker. |
| `ex_dividend_date.gte` | string | No | Range by ex_dividend_date. |
| `ex_dividend_date.gt` | string | No | Range by ex_dividend_date. |
| `ex_dividend_date.lte` | string | No | Range by ex_dividend_date. |
| `ex_dividend_date.lt` | string | No | Range by ex_dividend_date. |
| `record_date.gte` | string | No | Range by record_date. |
| `record_date.gt` | string | No | Range by record_date. |
| `record_date.lte` | string | No | Range by record_date. |
| `record_date.lt` | string | No | Range by record_date. |
| `declaration_date.gte` | string | No | Range by declaration_date. |
| `declaration_date.gt` | string | No | Range by declaration_date. |
| `declaration_date.lte` | string | No | Range by declaration_date. |
| `declaration_date.lt` | string | No | Range by declaration_date. |
| `pay_date.gte` | string | No | Range by pay_date. |
| `pay_date.gt` | string | No | Range by pay_date. |
| `pay_date.lte` | string | No | Range by pay_date. |
| `pay_date.lt` | string | No | Range by pay_date. |
| `cash_amount.gte` | number | No | Range by cash_amount. |
| `cash_amount.gt` | number | No | Range by cash_amount. |
| `cash_amount.lte` | number | No | Range by cash_amount. |
| `cash_amount.lt` | number | No | Range by cash_amount. |
| `order` | string | No | Order results based on the `sort` field. |
| `limit` | integer | No | Limit the number of results returned, default is 10 and max is 1000. |
| `sort` | string | No | Sort field used for ordering. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page of data. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].cash_amount` | number | The cash amount of the dividend per share owned. |
| `results[].currency` | string | The currency in which the dividend is paid. |
| `results[].declaration_date` | string | The date that the dividend was announced. |
| `results[].dividend_type` | enum: CD, SC, LT, ST | The type of dividend. Dividends that have been paid and/or are expected to be paid on consistent schedules are denoted as CD. Special Cash dividends that have been paid that are infrequent or unusual, and/or can not be expected to occur in the future are denoted as SC. Long-Term and Short-Term capital gain distributions are denoted as LT and ST, respectively. |
| `results[].ex_dividend_date` | string | The date that the stock first trades without the dividend, determined by the exchange. |
| `results[].frequency` | integer | The number of times per year the dividend is paid out. Possible values are 0 (one-time), 1 (annually), 2 (bi-annually), 4 (quarterly), 12 (monthly), 24 (bi-monthly), and 52 (weekly). |
| `results[].id` | string | The unique identifier of the dividend. |
| `results[].pay_date` | string | The date that the dividend is paid out. |
| `results[].record_date` | string | The date that the stock must be held to receive the dividend, set by the company. |
| `results[].ticker` | string | The ticker symbol of the dividend. |
| `status` | string | The status of this request's response. |
