# REST
## Futures

### Exchanges

**Endpoint:** `GET /futures/vX/exchanges`

**Description:**

Retrieve a list of supported futures exchanges, including their unique exchange codes, names, and other important details.

Use Cases: Exchange reference, market analysis, and compliance checks.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '999'. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].acronym` | string | Well-known acronym for the exchange (e.g., 'CME', 'NYMEX', 'CBOT', 'COMEX'). |
| `results[].id` | string | Numeric identifier for the futures exchange or trading venue. |
| `results[].locale` | string | Geographic location code where the exchange operates. |
| `results[].mic` | string | Market Identifier Code (MIC) - ISO 10383 standard four-character code for the futures market. |
| `results[].name` | string | Full official name of the futures exchange (e.g., 'Chicago Mercantile Exchange', 'New York Mercantile Exchange'). |
| `results[].operating_mic` | string | Operating Market Identifier Code for the futures exchange. |
| `results[].type` | string | Type of venue - 'exchange' for futures exchanges and derivatives trading platforms. |
| `results[].url` | string | Official website URL of the futures exchange organization. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "acronym": "CME",
      "id": "4",
      "locale": "US",
      "mic": "XCME",
      "name": "Chicago Mercantile Exchange",
      "operating_mic": "XCME",
      "type": "exchange",
      "url": "https://cmegroup.com"
    }
  ],
  "status": "OK"
}
```
