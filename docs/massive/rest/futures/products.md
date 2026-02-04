# REST
## Futures

### Products

**Endpoint:** `GET /futures/vX/products`

**Description:**

The Products API is a unified source for discovering all supported futures products and retrieving full product specifications. It returns the complete product universe with product codes, names, exchange identifiers, sector and asset class classifications, product type, settlement method, and pricing and quotation details. You can filter by name, exchange, sector, asset class, product type, or date to capture the product set or product definition that existed at a specific point in time. It also retrieves the full specification for a single product, supporting accurate system configuration, analytics, trading workflows, and historical reconciliation.

Use Cases: Product specification, historical product checks, risk management, trading system integration.

## Query Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `name` | string | No | The full name of the product. |
| `name.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `name.gt` | string | No | Filter greater than the value. |
| `name.gte` | string | No | Filter greater than or equal to the value. |
| `name.lt` | string | No | Filter less than the value. |
| `name.lte` | string | No | Filter less than or equal to the value. |
| `product_code` | string | No | The identifier for the product. |
| `product_code.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `product_code.gt` | string | No | Filter greater than the value. |
| `product_code.gte` | string | No | Filter greater than or equal to the value. |
| `product_code.lt` | string | No | Filter less than the value. |
| `product_code.lte` | string | No | Filter less than or equal to the value. |
| `date` | string | No | A date string in the format YYYY-MM-DD. This parameter will return point-in-time information about products for the specified day. Value must be formatted 'yyyy-mm-dd'. |
| `date.gt` | string | No | Filter greater than the value. Value must be formatted 'yyyy-mm-dd'. |
| `date.gte` | string | No | Filter greater than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `date.lt` | string | No | Filter less than the value. Value must be formatted 'yyyy-mm-dd'. |
| `date.lte` | string | No | Filter less than or equal to the value. Value must be formatted 'yyyy-mm-dd'. |
| `trading_venue` | string | No | The trading venue (MIC) for the exchange on which this product's contracts trade. |
| `trading_venue.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `trading_venue.gt` | string | No | Filter greater than the value. |
| `trading_venue.gte` | string | No | Filter greater than or equal to the value. |
| `trading_venue.lt` | string | No | Filter less than the value. |
| `trading_venue.lte` | string | No | Filter less than or equal to the value. |
| `sector` | string | No | The sector to which the product belongs. |
| `sector.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `sub_sector` | string | No | The sub-sector to which the product belongs. |
| `sub_sector.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `asset_class` | string | No | The asset class to which the product belongs. |
| `asset_class.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `asset_sub_class` | string | No | The asset sub-class to which the product belongs. |
| `asset_sub_class.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `type` | string | No | The type of product, one of 'single' or 'combo'. Leaving this filter blank will query for both 'single' and 'combo' types. |
| `type.any_of` | string | No | Filter equal to any of the values. Multiple values can be specified by using a comma separated list. |
| `limit` | integer | No | Limit the maximum number of results returned. Defaults to '100' if not specified. The maximum allowed limit is '50000'. |
| `sort` | string | No | A comma separated list of sort columns. For each column, append '.asc' or '.desc' to specify the sort direction. The sort column defaults to 'date' if not specified. The sort order defaults to 'asc' if not specified. |

## Response Attributes

| Field | Type | Description |
| --- | --- | --- |
| `next_url` | string | If present, this value can be used to fetch the next page. |
| `request_id` | string | A request id assigned by the server. |
| `results` | array[object] | The results for this request. |
| `results[].asset_class` | string | The asset class to which the product belongs. |
| `results[].asset_sub_class` | string | The asset sub-class to which the product belongs. |
| `results[].date` | string | A date string in the format YYYY-MM-DD. This parameter will return point-in-time information about products for the specified day. |
| `results[].last_updated` | string | The date and time at which this product was last updated. |
| `results[].name` | string | The full name of the product. |
| `results[].price_quotation` | string | The quoted price for this product. |
| `results[].product_code` | string | The identifier for the product. |
| `results[].sector` | string | The sector to which the product belongs. |
| `results[].settlement_currency_code` | string | The currency in which this product settles. |
| `results[].settlement_method` | string | The method of settlement for this product (Financially Settled or Deliverable). |
| `results[].settlement_type` | string | The type of settlement for this product. |
| `results[].sub_sector` | string | The sub-sector to which the product belongs. |
| `results[].trade_currency_code` | string | The currency in which this product's contracts trade. |
| `results[].trading_venue` | string | The trading venue (MIC) for the exchange on which this product's contracts trade. |
| `results[].type` | string | The type of product, one of 'single' or 'combo'. Leaving this filter blank will query for both 'single' and 'combo' types. |
| `results[].unit_of_measure` | string | The unit of measure for this product. |
| `results[].unit_of_measure_qty` | number | The quantity of the unit of measure for this product. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "next_url": "https://api.massive.com/futures/vX/products?cursor=YXA9MTAwJmFzPSZhc19vZj0yMDI1LTA3LTA3JmxpbWl0PTEwMCZzb3J0PW5hbWUuYXNj",
  "request_id": "000a000a0a0a000a0a0aa00a0a0000a0",
  "results": [
    {
      "asset_class": "commodity",
      "asset_sub_class": "energy",
      "date": "2025-07-07",
      "last_updated": "2025-02-22",
      "name": "1% Fuel Oil Barges FOB Rdam (Platts) vs. 1% Fuel Oil Cargoes FOB NWE (Platts) BALMO Futures",
      "price_quotation": "U.S. dollars and cents per metric ton",
      "product_code": "EBE",
      "sector": "refined_products",
      "settlement_currency_code": "USD",
      "settlement_method": "financially_settled",
      "settlement_type": "cash",
      "sub_sector": "european",
      "trade_currency_code": "USD",
      "trading_venue": "XNYM",
      "type": "single",
      "unit_of_measure": "MTONS",
      "unit_of_measure_qty": 1000
    }
  ],
  "status": "OK"
}
```
