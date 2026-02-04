# REST
## Partners

### ETF Profiles & Exposure

**Endpoint:** `GET /etf-global/v1/profiles`

**Description:**

Retrieve industry classification data for global ETFs including sector mappings and standardized categorizations across industry frameworks.

Use Cases: ETF categorization and classification, sector and geographic exposure analysis, fund profiling and comparison, portfolio due diligence.

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
| `results[].administrator` | string | The administrator of the ETF. |
| `results[].advisor` | string | The investment advisor of the ETF. |
| `results[].asset_class` | string | The primary type of assets held by the ETF, such as equities, bonds, commodities, or other securities. |
| `results[].aum` | number | The total assets under management, representing the current market value of all assets held by the ETF. |
| `results[].avg_daily_trading_volume` | number | The average number of shares traded daily over the past month, indicating liquidity and investor interest. |
| `results[].bid_ask_spread` | number | The average intraday bid-ask spread as a percentage, calculated by dividing the spread by the lowest ask price sampled during the day. |
| `results[].call_volume` | number | Call options volume. |
| `results[].category` | string | The broad investment category that describes the ETF's investment focus and strategy. |
| `results[].composite_ticker` | string | The stock ticker symbol used to identify this ETF product on exchanges. |
| `results[].coupon_exposure` | array[object] | Coupon exposure breakdown for fixed income ETFs. |
| `results[].creation_fee` | number | The fee for creating new shares of the ETF. |
| `results[].creation_unit_size` | number | The size of creation units for the ETF. |
| `results[].currency_exposure` | array[object] | Currency exposure breakdown of the ETF. |
| `results[].custodian` | string | The custodian of the ETF assets. |
| `results[].description` | string | The official name and description of the ETF product. |
| `results[].development_class` | string | The economic development classification of the markets the ETF invests in, such as developed, emerging, or frontier markets. |
| `results[].discount_premium` | number | Discount or premium to net asset value. |
| `results[].distribution_frequency` | string | How frequently the ETF makes distributions. |
| `results[].distributor` | string | The distributor of the ETF. |
| `results[].effective_date` | string | The date showing when the information was accurate or valid; some issuers, such as Vanguard, release their data on a delay, so the effective_date can be several weeks earlier than the processed_date. |
| `results[].fee_waivers` | number | Any fee waivers applied to the ETF. |
| `results[].fiscal_year_end` | string | The fiscal year end date for the ETF. |
| `results[].focus` | string | The specific investment focus or exposure that the ETF provides, such as sector, geography, or investment style. |
| `results[].futures_commission_merchant` | string | The futures commission merchant, if applicable. |
| `results[].geographic_exposure` | array[object] | Geographic exposure breakdown of the ETF. |
| `results[].inception_date` | string | The date when this ETF was first launched and became available for trading. |
| `results[].industry_exposure` | array[object] | Industry exposure breakdown of the ETF. |
| `results[].industry_group_exposure` | array[object] | Industry group exposure breakdown of the ETF. |
| `results[].issuer` | string | The financial institution or fund company that created and sponsors this ETF. |
| `results[].lead_market_maker` | string | The lead market maker for the ETF. |
| `results[].leverage_style` | string | Indicates whether the ETF uses leverage to amplify returns ('leveraged'), or does not use leverage ('unleveraged'). |
| `results[].levered_amount` | number | The leverage multiplier applied by the ETF, where positive numbers indicate leveraged exposure and negative numbers indicate inverse exposure. |
| `results[].listing_exchange` | string | The primary exchange where the ETF is listed. |
| `results[].management_classification` | string | Defines whether an ETF is considered active under SEC rules, with managers making investment decisions, or passive, tracking an index. |
| `results[].management_fee` | number | The annual fee charged by the fund manager for managing the ETF's portfolio and operations. |
| `results[].maturity_exposure` | array[object] | Maturity exposure breakdown for fixed income ETFs. |
| `results[].net_expenses` | number | Net expenses after waivers. |
| `results[].num_holdings` | number | Number of holdings in the ETF. |
| `results[].options_available` | integer | Availability of options on the ETF. |
| `results[].options_volume` | number | Options trading volume for the ETF. |
| `results[].other_expenses` | number | Other expenses charged by the ETF. |
| `results[].portfolio_manager` | string | The portfolio manager of the ETF. |
| `results[].primary_benchmark` | string | The main index or benchmark that this ETF is designed to track or replicate. |
| `results[].processed_date` | string | The date showing when ETF Global received and processed the data. |
| `results[].product_type` | string | Indicates whether the product is an Exchange-Traded Note ('etn') or an Exchange-Traded Fund ('etf'). |
| `results[].put_call_ratio` | number | Put/call ratio for options on the ETF. |
| `results[].put_volume` | number | Put options volume. |
| `results[].region` | string | The geographic region or area of the world where the ETF concentrates its investments. |
| `results[].sector_exposure` | array[object] | Sector exposure breakdown of the ETF. |
| `results[].short_interest` | number | Short interest in the ETF. |
| `results[].subadvisor` | string | The subadvisor of the ETF, if applicable. |
| `results[].subindustry_exposure` | array[object] | Sub-industry exposure breakdown of the ETF. |
| `results[].tax_classification` | string | The tax structure of the ETF, determining whether investors receive 1099 or K1 tax forms (RIC, Partnership, or UIT). |
| `results[].total_expenses` | number | The total annual expense ratio of the ETF, including all fees and costs passed on to investors. |
| `results[].transfer_agent` | string | The transfer agent for the ETF. |
| `results[].trustee` | string | The trustee of the ETF. |
| `status` | enum: OK | The status of this request's response. |

## Sample Response

```json
{
  "count": 1,
  "request_id": 1,
  "results": [
    {
      "administrator": "State Street Bank and Trust Company",
      "advisor": "SSgA Funds Management, Inc.",
      "asset_class": "Equity",
      "aum": 624531939442.66,
      "avg_daily_trading_volume": 51287737.3,
      "bid_ask_spread": 0.000042,
      "call_volume": 4797339,
      "category": "Size and Style",
      "composite_ticker": "SPY",
      "creation_fee": 3000,
      "creation_unit_size": 50000,
      "currency_exposure": {
        "usd": 1.003
      },
      "custodian": "State Street Bank and Trust Company",
      "description": "SPDR S&P 500 ETF Trust",
      "development_class": "Developed Markets",
      "discount_premium": 0.136123,
      "distribution_frequency": "Q",
      "distributor": "ALPS Distributors, Inc.",
      "effective_date": "2025-01-02",
      "fee_waivers": 0,
      "fiscal_year_end": "31-Aug",
      "focus": "Large Cap",
      "geographic_exposure": {
        "bm": 0.001,
        "ch": 0.003,
        "ie": 0.021,
        "je": 0.001,
        "lr": 0.001,
        "nl": 0.001,
        "pa": 0.001,
        "us": 0.967
      },
      "inception_date": "1993-01-22",
      "industry_exposure": {
        "aerospace_and_defense": 0.011,
        "air_freight_and_logistics": 0.004,
        "airlines": 0.002,
        "auto_components": 0,
        "automobiles": 0.024,
        "banks": 0.033,
        "beverages": 0.011,
        "biotechnology": 0.047,
        "building_products": 0.002,
        "capital_markets": 0.029,
        "cash_or_derivatives": 0.003,
        "chemicals": 0.007,
        "commercial_services_and_supplies": 0.004,
        "communications_equipment": 0.081,
        "construction_and_engineering": 0.001,
        "construction_materials": 0.001,
        "consumer_products": 0.001,
        "containers_and_packaging": 0.001,
        "distributors": 0.001,
        "diversified_consumer_services": 0.005,
        "diversified_financial_services": 0.029,
        "diversified_telecommunication_services": 0.009,
        "electrical_equipment": 0.009,
        "electronic_equipment_instruments_and_components": 0.001,
        "entertainment": 0.008,
        "equity_real_estate_investment": 0.001,
        "food_products": 0.006,
        "health_care_equipment_and_supplies": 0.031,
        "health_care_providers_and_services": 0.018,
        "health_care_technology": 0.003,
        "hotels,_restaurants_and_leisure": 0.001,
        "hotels_restaurants_and_leisure": 0.014,
        "household_durables": 0.004,
        "household_products": 0.011,
        "industrial_conglomerates": 0.005,
        "insurance": 0.035,
        "it_services": 0.021,
        "leisure_products": 0,
        "machinery": 0.013,
        "media": 0.077,
        "metals_and_mining": 0.003,
        "oil_gas_and_consumable_fuels": 0.031,
        "real_estate_management_and_development": 0.018,
        "renewable_energy": 0.001,
        "road_and_rail": 0.003,
        "semiconductors_and_semiconductor_equipment": 0.115,
        "software": 0.101,
        "specialty_retail": 0.082,
        "technology_hardware_storage_and_peripherals": 0.001,
        "textiles_apparel_and_luxury_goods": 0.003,
        "tobacco": 0.006,
        "trading_companies_and_distributors": 0.002,
        "transportation_infrastructure": 0.006,
        "utilities": 0.022
      },
      "industry_group_exposure": {
        "automobiles_and_components": 0.024,
        "banks": 0.033,
        "capital_goods": 0.045,
        "cash_or_derivatives": 0.003,
        "commercial_and_professional_services": 0.005,
        "consumer_durables_and_apparel": 0.007,
        "consumer_services": 0.02,
        "diversified_financials": 0.058,
        "energy": 0.032,
        "food_and_staples_retailing": 0.018,
        "food_beverage_and_tobacco": 0.023,
        "health_care_equipment_and_services": 0.046,
        "household_and_personal_products": 0.011,
        "insurance": 0.035,
        "materials": 0.013,
        "media_and_entertainment": 0.084,
        "pharmaceuticals_biotechnology_and_life_sciences": 0.053,
        "real_estate": 0.02,
        "retailing": 0.065,
        "semiconductors_and_semiconductor_equipment": 0.115,
        "software_and_services": 0.12,
        "technology_hardware_and_equipment": 0.084,
        "telecommunication_services": 0.009,
        "transportation": 0.002,
        "transportation_and_logistics": 0.012,
        "utilities": 0.022
      },
      "issuer": "SSgA",
      "lead_market_maker": "None",
      "leverage_style": "unleveraged",
      "levered_amount": 0,
      "listing_exchange": "NYSE Arca, Inc.",
      "management_classification": "passive",
      "management_fee": 0.0945,
      "net_expenses": 0.0945,
      "num_holdings": 504,
      "options_available": 1,
      "options_volume": 9346839,
      "other_expenses": 0,
      "primary_benchmark": "S&P 500 Index",
      "processed_date": "2025-01-02",
      "product_type": "etf",
      "put_call_ratio": 0.948338,
      "put_volume": 4549500,
      "region": "North America",
      "sector_exposure": {
        "cash_or_derivatives": 0.003,
        "communications": 0.094,
        "consumer_discretionary": 0.113,
        "consumer_staples": 0.054,
        "energy": 0.032,
        "financials": 0.131,
        "health_care": 0.099,
        "industrials": 0.068,
        "materials": 0.013,
        "real_estate": 0.02,
        "technology": 0.321,
        "utilities": 0.022
      },
      "short_interest": 106750000,
      "subindustry_exposure": {
        "advertising": 0.001,
        "aerospace_and_defense": 0.011,
        "agricultural_and_farm_machinery": 0.002,
        "agricultural_products": 0.001,
        "air_freight_and_logistics": 0.004,
        "airlines": 0.002,
        "alternative_carriers": 0.009,
        "apparel_accessories_and_luxury": 0.003,
        "apparel_retail": 0.005,
        "application_software": 0.024,
        "asset_management_and_custody_banks": 0.006,
        "auto_parts_and_equipment": 0,
        "automobile_manufacturers": 0.024,
        "automotive_retail": 0.004,
        "biotechnology": 0.047,
        "brewers": 0,
        "building_products": 0.002,
        "cable_and_satellite": 0.004,
        "cash_or_derivatives": 0.003,
        "casinos_and_gaming": 0.001,
        "commodity_chemicals": 0.001,
        "communications_equipment": 0.081,
        "construction_and_engineering": 0.001,
        "construction_machinery_and_heavy_trucks": 0.006,
        "construction_materials": 0.001,
        "consumer_electronics": 0.001,
        "consumer_finance": 0.029,
        "data_processing_and_outsourced_services": 0.006,
        "distillers_and_vintners": 0.001,
        "distributors": 0,
        "diversified_banks": 0.033,
        "diversified_chemicals": 0.002,
        "diversified_metals_and_mining": 0.001,
        "diversified_support_services": 0.001,
        "drug_retail": 0,
        "electric_utilities": 0.013,
        "electrical_components_and_equipment": 0.009,
        "electronic_components": 0.001,
        "electronic_equipment_and_instruments": 0,
        "electronic_manufacturing_services": 0,
        "environmental_and_facilities_services": 0.003,
        "fertilizers_and_agricultural_che": 0.001,
        "financial_exchanges_and_data": 0.006,
        "food_retail": 0.002,
        "gas_utilities": 0,
        "general_merchandise_stores": 0.002,
        "health_care_distributors": 0.003,
        "health_care_equipment": 0.014,
        "health_care_facilities": 0.001,
        "health_care_services": 0.004,
        "health_care_supplies": 0.003,
        "heavy_electrical_equipment": 0.001,
        "highways_and_railtracks": 0.005,
        "home_improvement_retail": 0.012,
        "homebuilding": 0.002,
        "hotels_resorts_and_cruise_lines": 0.004,
        "household_products": 0.011,
        "hypermarkets_and_super_centers": 0.016,
        "independent_power_producers_and_energy_traders": 0.001,
        "industrial_conglomerates": 0.005,
        "industrial_machinery": 0.005,
        "insurance_brokers": 0.005,
        "integrated_oil_and_gas": 0.014,
        "interactive_media_and_services": 0.067,
        "internet_and_direct_marketing_retail": 0.043,
        "internet_services_and_infrastruc": 0.004,
        "investment_banking_and_brokerage": 0.01,
        "it_consulting_and_other_services": 0.011,
        "leisure_facilities": 0,
        "leisure_products": 0.001,
        "life_and_health_insurance": 0.003,
        "life_sciences_tools_and_services": 0.014,
        "managed_health_care": 0.013,
        "metal_and_glass_containers": 0.001,
        "movies_and_entertainment": 0.012,
        "multiutilities": 0.006,
        "oil_and_gas_equipment_and_services": 0.002,
        "oil_and_gas_exploration_and_production": 0.007,
        "oil_and_gas_refining_and_marketing": 0.003,
        "oil_and_gas_storage_and_transporta": 0.004,
        "packaged_foods_and_meats": 0.006,
        "paper_packaging": 0.001,
        "precious_metals_and_minerals": 0.001,
        "property_and_casualty_insurance": 0.027,
        "publishing_and_broadcasting": 0,
        "railroads": 0.001,
        "real_estate_services": 0.001,
        "reinsurance": 0,
        "reit": 0.019,
        "renewable_energy_equipment": 0.001,
        "research_and_consulting_services": 0.002,
        "restaurants": 0.009,
        "security_and_alarm_services": 0.004,
        "semiconductors": 0.115,
        "soft_drinks": 0.01,
        "specialty_chemicals": 0.004,
        "specialty_stores": 0.001,
        "steel": 0.001,
        "systems_software": 0.078,
        "technology_hardware_storage_and_peripherals": 0.002,
        "tobacco": 0.006,
        "trading_companies_and_distributors": 0.002,
        "trucking": 0.004,
        "water_utilities": 0
      },
      "tax_classification": "Regulated Investment Company",
      "total_expenses": 0.0945,
      "transfer_agent": "State Street Bank and Trust Company",
      "trustee": "State Street Global Advisors Trust Company"
    }
  ],
  "status": "OK"
}
```
