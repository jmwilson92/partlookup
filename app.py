from flask import Flask, render_template, request
import requests
import json
import logging

app = Flask(__name__)

# Logging setup
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("debug.log"), logging.StreamHandler()]
)

# API credentials
DIGIKEY_CLIENT_ID = "SLkBK6RsgWvSPcSeylYP8MrU728LDGZi"
DIGIKEY_CLIENT_SECRET = "lErAjshjJFsVJcCi"
DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_API_URL = "https://api.digikey.com/products/v4/search/keyword"

MOUSER_API_KEY = "6e28c540-f192-46bf-878d-1f1f97787bfa"
MOUSER_API_URL = "https://api.mouser.com/api/v1/search/partnumber"

SUPPLIER_SCORES = {"DigiKey": 4.7, "Mouser": 4.5}
RISKY_REGIONS = ["CN", "RU"]

# Dummy disruptions for tracker
DISRUPTIONS = [
    {"id": 1, "location": "Port of Los Angeles", "industry": "Manufacturing", "issue": "Shipping Delay", "severity": "High"},
    {"id": 2, "location": "Shanghai", "industry": "Electronics", "issue": "Component Shortage", "severity": "Medium"},
    {"id": 3, "location": "Texas", "industry": "Agriculture", "issue": "Weather Disruption", "severity": "Low"}
]

def get_digikey_token():
    payload = {"grant_type": "client_credentials", "client_id": DIGIKEY_CLIENT_ID, "client_secret": DIGIKEY_CLIENT_SECRET}
    try:
        response = requests.post(DIGIKEY_TOKEN_URL, data=payload, timeout=5)
        response.raise_for_status()
        logging.info("DigiKey token retrieved")
        return response.json()["access_token"]
    except requests.exceptions.RequestException as e:
        logging.error(f"DigiKey token failed: {str(e)}")
        raise

def get_digikey_data(part_number):
    try:
        token = get_digikey_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": DIGIKEY_CLIENT_ID,
            "X-DIGIKEY-Locale-Site": "US",
            "X-DIGIKEY-Locale-Language": "en",
            "X-DIGIKEY-Locale-Currency": "USD",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        variants = [part_number]
        if part_number == "640456-5":
            variants.append("A19471-ND")
        elif part_number == "433-1028-ND":
            variants.append("0039012040")
        elif part_number == "10165968-113Y000LF":
            variants.append("609-11109-ND")
        elif part_number == "WT-1205":
            variants.append("433-1028-ND")

        for variant in variants:
            logging.info(f"DigiKey searching variant: {variant}")
            payload = {"keywords": variant, "limit": 10, "offset": 0}
            response = requests.post(DIGIKEY_API_URL, headers=headers, json=payload, timeout=5)
            response.raise_for_status()
            raw_response = response.text
            logging.info(f"DigiKey raw response text: {raw_response}")
            try:
                data = response.json()
            except json.JSONDecodeError as e:
                logging.error(f"DigiKey JSON decode failed: {str(e)} - Using raw response")
                data = json.loads(raw_response)
            logging.info(f"DigiKey parsed data: {data}")
            products_count = data.get("ProductsCount", 0)
            products = data.get("Products", [])
            logging.info(f"DigiKey productsCount: {products_count}, products exists: {bool(products)}")
            if products_count > 0 and products:
                logging.info(f"DigiKey found: {products[0].get('ManufacturerProductNumber')}")
                return products[0], f"DigiKey API: Searched for '{part_number}' (matched '{variant}')"
            else:
                correlation_id = response.headers.get("X-Correlation-ID", "Not provided")
                logging.warning(f"DigiKey returned 0 products for '{variant}' - Correlation ID: {correlation_id}")
        logging.info("DigiKey no results after all variants")
        return None, f"DigiKey API: No results for '{part_number}' - Last Response: {response.text}"
    except Exception as e:
        logging.error(f"DigiKey error: {str(e)}")
        return None, f"DigiKey API: Error - {str(e)}"

def get_mouser_data(part_number):
    try:
        url = f"{MOUSER_API_URL}?apiKey={MOUSER_API_KEY}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        payload = {"SearchByPartRequest": {"mouserPartNumber": part_number, "partSearchOptions": "Exact"}}
        response = requests.post(url, headers=headers, json=payload, timeout=5)
        response.raise_for_status()
        data = response.json()
        logging.info(f"Mouser response: {data}")
        if not data.get("Errors") and data.get("SearchResults", {}).get("NumberOfResult", 0) > 0:
            return data["SearchResults"]["Parts"][0], f"Mouser API: Searched for '{part_number}'"
        return None, f"Mouser API: No results for '{part_number}'"
    except Exception as e:
        logging.error(f"Mouser error: {str(e)}")
        return None, f"Mouser API: Error - {str(e)}"

def get_part_info(part_number, custom_url=None):
    result = {
        "part_number": part_number,
        "manufacturer": "Unknown",
        "description": "Not available",
        "category": "Unknown",
        "suppliers": [],
        "lead_time": "Not specified",
        "lifecycle_status": "Unknown",
        "current_events": "No relevant current events found.",
        "sources_checked": [],
        "availability": "Checking...",
        "single_sourced": "Unknown",
        "high_risk_suppliers": [],
        "lead_times": {},
        "cost_alternatives": [],
        "obsolescence_risk": "Unknown",
        "bottlenecks": "None identified",
        "disruption_exposure": "Low",
        "quality_standards": "Assumed compliant",
        "substitution_flexibility": "Unknown",
        "total_landed_cost": "Calculating...",
        "inventory_alignment": "Unknown",
        "sustainability_concerns": "None identified"
    }

    distributors = [
        {"name": "DigiKey", "fetch": get_digikey_data},
        {"name": "Mouser", "fetch": get_mouser_data}
    ]

    supplier_data_list = []
    lead_time_set = False
    for dist in distributors:
        part_data, source_message = dist["fetch"](part_number)
        result["sources_checked"].append(source_message)

        if part_data:
            if result["manufacturer"] == "Unknown":
                manufacturer = part_data.get("manufacturer") or part_data.get("Manufacturer") or part_data.get("ManufacturerName")
                if isinstance(manufacturer, dict):
                    result["manufacturer"] = manufacturer.get("name", manufacturer.get("Value", "Unknown"))
                else:
                    result["manufacturer"] = manufacturer if manufacturer else "Unknown"
                
                result["description"] = part_data.get("description", part_data.get("Description", part_data.get("ProductDescription", "Not available")))
                category = part_data.get("category", part_data.get("Category", part_data.get("CategoryName")))
                if isinstance(category, dict):
                    result["category"] = category.get("name", category.get("Value", "Unknown"))
                else:
                    result["category"] = category if category else "Unknown"
                
                result["lifecycle_status"] = part_data.get("productStatus", part_data.get("LifecycleStatus", part_data.get("ProductStatus", "Unknown")))

            supplier_data = {
                "name": dist["name"],
                "availability": "Unknown",
                "lead_time": "Not specified",
                "price_breaks": "Not available",
                "moq": "Unknown",
                "score": SUPPLIER_SCORES.get(dist["name"], 0.0)
            }
            stock = part_data.get("quantityAvailable", part_data.get("QuantityAvailable", part_data.get("Availability", 0)))
            if isinstance(stock, str):
                stock = int(stock.split()[0].replace(",", ""))
            if stock > 0:
                supplier_data["availability"] = f"{stock} In Stock"
                lead_time = part_data.get("factoryLeadTime", part_data.get("ManufacturerLeadWeeks", part_data.get("LeadTime", 0)))
                if isinstance(lead_time, str):
                    lead_time = int(''.join(filter(str.isdigit, lead_time))) if any(c.isdigit() for c in lead_time) else 0
                supplier_data["lead_time"] = f"{lead_time} weeks" if lead_time else "Not specified"
                if not lead_time_set and lead_time:
                    result["lead_time"] = supplier_data["lead_time"]
                    lead_time_set = True
                price_breaks = part_data.get("standardPricing", part_data.get("PriceBreaks", part_data.get("PriceList", [])))
                if price_breaks:
                    supplier_data["price_breaks"] = ", ".join(
                        f"{pb['breakQuantity' if 'breakQuantity' in pb else 'Quantity']}: ${float(pb['unitPrice' if 'unitPrice' in pb else 'Price'].replace('$', '').replace(',', '')):.2f}"
                        for pb in price_breaks
                    )
                    supplier_data["moq"] = str(min(pb["breakQuantity"] if "breakQuantity" in pb else pb["Quantity"] for pb in price_breaks))
                result["suppliers"].append(supplier_data)
                supplier_data_dict = {
                    "name": dist["name"],
                    "stock": stock,
                    "lead_time": lead_time,
                    "region": "US"
                }
                if price_breaks:
                    supplier_data_dict["min_price"] = float(price_breaks[0]["unitPrice"] if "unitPrice" in price_breaks[0] else price_breaks[0]["Price"].replace('$', '').replace(',', ''))
                supplier_data_list.append(supplier_data_dict)

    # BOM Analysis
    supplier_count = len(supplier_data_list)
    result["availability"] = "Yes" if supplier_count > 0 and all(s["stock"] > 0 for s in supplier_data_list) else "No"
    result["single_sourced"] = "Yes" if supplier_count == 1 else "No" if supplier_count > 1 else "Unknown"
    result["high_risk_suppliers"] = [s["name"] for s in supplier_data_list if s["region"] in RISKY_REGIONS]
    result["lead_times"] = {s["name"]: s["lead_time"] for s in supplier_data_list}
    result["cost_alternatives"] = sorted(
        [{"supplier": s["name"], "min_price": s["min_price"]} for s in supplier_data_list if "min_price" in s],
        key=lambda x: x["min_price"]
    ) if any("min_price" in s for s in supplier_data_list) else []
    result["obsolescence_risk"] = "Yes" if result["lifecycle_status"] in ["EndOfLife", "Discontinued"] else "No"
    result["bottlenecks"] = "Potential" if supplier_count == 1 else "None identified"
    result["disruption_exposure"] = "High" if result["high_risk_suppliers"] else "Low"
    result["quality_standards"] = "Assumed compliant"
    result["substitution_flexibility"] = "Yes" if supplier_count > 1 else "No"
    result["total_landed_cost"] = sum(s["min_price"] for s in supplier_data_list if "min_price" in s) + 0.10 * supplier_count if any("min_price" in s for s in supplier_data_list) else "Unknown"
    result["inventory_alignment"] = "Unknown"
    result["sustainability_concerns"] = "Potential" if result["high_risk_suppliers"] else "None identified"

    # Current events
    category = result["category"].lower()
    manufacturer = result["manufacturer"].lower()
    events = []
    if "headers" in category or "connectors" in category:
        events.append("Proposed U.S. tariffs in 2025 (25% on Canada/Mexico, 10-60% on China) could raise costs for electronic components, especially connectors and headers.")
    if "molex" in manufacturer.lower() or "amphenol" in manufacturer.lower():
        events.append(f"{manufacturer.capitalize()} may face supply chain pressures due to tariffs impacting connector production.")
    events.append("Global semiconductor shortages and 2024 disruptions may persist into 2025.")
    result["current_events"] = " ".join(events) if events else "No relevant current events found."

    return result

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        part_number = request.form["part_number"]
        custom_url = request.form.get("custom_url", None)
        result = get_part_info(part_number, custom_url)
        return render_template("result.html", data=result)
    return render_template("index.html")

@app.route("/disruptions")
def disruptions():
    industry_filter = request.args.get('industry', 'All')
    if industry_filter == 'All':
        filtered_disruptions = DISRUPTIONS
    else:
        filtered_disruptions = [d for d in DISRUPTIONS if d['industry'] == industry_filter]
    industries = ['All', 'Manufacturing', 'Electronics', 'Agriculture']
    return render_template("disruptions.html", disruptions=filtered_disruptions, industries=industries)

if __name__ == "__main__":
    app.run(debug=True)