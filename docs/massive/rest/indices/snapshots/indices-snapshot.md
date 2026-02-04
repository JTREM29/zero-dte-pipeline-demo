# REST
## Indices

### Indices Snapshot

**Endpoint:** `GET /v3/snapshot/indices`

**Description:**

Retrieve snapshot data for one or more indices, including their current value, recent performance metrics, and trading session details. By consolidating key information for each specified index, this endpoint helps users assess market conditions, track broad economic sentiment, and integrate index-level data into trading or analysis workflows.

Use Cases: Market condition assessment, economic sentiment tracking, portfolio context, and integrated index data analysis.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `ticker.any_of` | string | No | Comma separated list of tickers, up to a maximum of 250. If no tickers are passed then all results will be returned in a paginated manner.  Warning: The maximum number of characters allowed in a URL are subject to your technology stack. |
| `ticker` | string | No | Search a range of tickers lexicographically. |
| `ticker.gte` | string | No | Range by ticker. |
| `ticker.gt` | string | No | Range by ticker. |
| `ticker.lte` | string | No | Range by ticker. |
| `ticker.lt` | string | No | Range by ticker. |
| `order` | string | No | Order results based on the `sort` field. |
| `limit` | integer | No | Limit the number of results returned, default is 10 and max is 250. |
| `sort` | string | No | Sort field used for ordering. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page of data. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | An array of results containing the requested data. |
| `results[].error` | string | The error while looking for this ticker. |
| `results[].last_updated` | integer | The nanosecond timestamp of when this information was updated. |
| `results[].market_status` | string | The market status for the market that trades this ticker. |
| `results[].message` | string | The error message while looking for this ticker. |
| `results[].name` | string | Name of Index. |
| `results[].session` | object | Trading session metrics, detailing change percentages and key price points (open, close, high, low) for the asset within the current trading day. |
| `results[].ticker` | string | Ticker of asset queried. |
| `results[].timeframe` | enum: DELAYED, REAL-TIME | The time relevance of the data. |
| `results[].type` | enum: indices | The indices market. |
| `results[].value` | number | Value of Index. |
| `status` | string | The status of this request's response. |

## Sample Response

```json
{
  "request_id": "6a7e466379af0a71039d60cc78e72282",
  "results": [
    {
      "last_updated": 1679597116344223500,
      "market_status": "closed",
      "name": "Dow Jones Industrial Average",
      "session": {
        "change": -50.01,
        "change_percent": -1.45,
        "close": 3822.39,
        "high": 3834.41,
        "low": 38217.11,
        "open": 3827.38,
        "previous_close": 3812.19
      },
      "ticker": "I:DJI",
      "timeframe": "REAL-TIME",
      "type": "indices",
      "value": 3822.39
    },
    {
      "error": "NOT_FOUND",
      "message": "Ticker not found.",
      "ticker": "APx"
    },
    {
      "error": "NOT_ENTITLED",
      "message": "Not entitled to this ticker.",
      "ticker": "APy"
    }
  ],
  "status": "OK"
}
```
