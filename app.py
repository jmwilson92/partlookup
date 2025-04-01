from flask import Flask, render_template, request, send_file, redirect, url_for, Response, session
from flask_sqlalchemy import SQLAlchemy
import requests
import json
import logging
import csv
from io import StringIO, BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from datetime import datetime

app = Flask(__name__)
app.secret_key = '540604112029342a864840d1d2f840788987ae190c037b4f'

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
DIGIKEY_SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"
MOUSER_API_KEY = "6e28c540-f192-46bf-878d-1f1f97787bfa"
MOUSER_API_URL = "https://api.mouser.com/api/v1/search/partnumber"
NEWS_API_KEY = "YOUR_NEWSAPI_KEY_HERE"  # Replace with your NewsAPI key

# Constants
RISKY_REGIONS = ["CN", "RU"]
SUBSTITUTIONS = {"640456-5": "A19471-ND"}

# SQLite setup
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///parts.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database models
class PartTrack(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    part_num = db.Column(db.String(50))
    price = db.Column(db.Float)
    stock = db.Column(db.Integer)
    timestamp = db.Column(db.String(20))
    risk_score = db.Column(db.Float)

class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    part_num = db.Column(db.String(50))
    comment = db.Column(db.Text)

class FavoritePart(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    part_num = db.Column(db.String(50), unique=True)
    critical = db.Column(db.Boolean, default=False)

class SupplierResponse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    supplier = db.Column(db.String(50))
    part_num = db.Column(db.String(50))
    response_time = db.Column(db.Float)

class Substitutions(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    part_num = db.Column(db.String(50))
    substitute_part_num = db.Column(db.String(50))

# Initialize DB
with app.app_context():
    db.drop_all()
    db.create_all()
    for p, s in SUBSTITUTIONS.items():
        if not Substitutions.query.filter_by(part_num=p).first():
            db.session.add(Substitutions(part_num=p, substitute_part_num=s))
    db.session.commit()
    logging.info("Database recreated successfully.")

def get_digikey_token():
    payload = {"grant_type": "client_credentials", "client_id": DIGIKEY_CLIENT_ID, "client_secret": DIGIKEY_CLIENT_SECRET}
    try:
        response = requests.post(DIGIKEY_TOKEN_URL, data=payload, timeout=5)
        response.raise_for_status()
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
        payload = {"keywords": part_number, "limit": 10, "offset": 0}
        response = requests.post(DIGIKEY_SEARCH_URL, headers=headers, json=payload, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get("ProductsCount", 0) > 0 and data.get("Products", []):
            product = data["Products"][0]
            coo = product.get("CountryOfOrigin", "US")
            hts = product.get("HTSCode", "8536.69")
            tariff_active = product.get("ProductVariations", [{}])[0].get("TariffActive", False)
            logging.debug(f"DigiKey Search response for {part_number}: {json.dumps(product, indent=2)}")
            return product, f"DigiKey API: Searched for '{part_number}'", product.get("ProductUrl", "https://www.digikey.com"), coo, hts, tariff_active
        return None, f"DigiKey API: No results for '{part_number}'", "https://www.digikey.com", "US", "8536.69", False
    except requests.exceptions.RequestException as e:
        logging.error(f"DigiKey Search error: {str(e)}")
        return None, f"DigiKey API: Error - {str(e)}", "https://www.digikey.com", "US", "8536.69", False

def get_mouser_data(part_number):
    try:
        url = f"{MOUSER_API_URL}?apiKey={MOUSER_API_KEY}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        payload = {"SearchByPartRequest": {"mouserPartNumber": part_number, "partSearchOptions": "Exact"}}
        response = requests.post(url, headers=headers, json=payload, timeout=5)
        response.raise_for_status()
        data = response.json()
        logging.debug(f"Mouser raw response for {part_number}: {json.dumps(data, indent=2)}")
        if not data.get("Errors") and data.get("SearchResults", {}).get("NumberOfResult", 0) > 0:
            part = data["SearchResults"]["Parts"][0]
            coo = part.get("CountryOfOrigin", "US")
            hts = part.get("HTSCode", "8536.69")
            surcharge_messages = part.get("SurchargeMessages") or []  # Force empty list if None
            tariff_active = any(msg.get("code") == "TARIFF_US_301" for msg in surcharge_messages)
            stock = part.get("QuantityAvailable", 0)
            if stock == 0 and part.get("Availability"):
                try:
                    stock = int(part["Availability"].split()[0].replace(",", ""))
                except (ValueError, IndexError):
                    stock = 0
            return part, f"Mouser API: Searched for '{part_number}'", part.get("ProductDetailUrl", "https://www.mouser.com"), coo, hts, tariff_active, stock
        return None, f"Mouser API: No results for '{part_number}'", "https://www.mouser.com", "US", "8536.69", False, 0
    except requests.exceptions.RequestException as e:
        logging.error(f"Mouser error for {part_number}: {str(e)}")
        return None, f"Mouser API: Error - {str(e)}", "https://www.mouser.com", "US", "8536.69", False, 0

def calculate_supplier_score(supplier_data, api_success):
    ease = 1 if api_success else 0
    availability = 2 if supplier_data["stock"] > 0 else 0
    price_breaks = len(supplier_data["price_breaks"].split(", ")) * 0.5 if supplier_data["price_breaks"] != "Not available" else 0
    lead_time = int(''.join(filter(str.isdigit, supplier_data["lead_time"]))) if supplier_data["lead_time"] != "Not specified" else 40
    lead_time_score = max(0, 10 - lead_time / 4)
    moq = int(supplier_data["moq"]) if supplier_data["moq"] != "Unknown" else 500
    moq_score = max(0, 5 - moq / 100)
    total = ease + availability + price_breaks + lead_time_score + moq_score
    return min(total / 4, 5)

def get_current_events(category, supplier_regions):
    try:
        url = f"https://newsapi.org/v2/everything?q={category}+%22supply+chain%22+electronics+-optics+-sensor+-battery+-network&sortBy=relevance&apiKey={NEWS_API_KEY}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", [])
        if articles:
            events = [article["title"] for article in articles[:3]]
            return " | ".join(events)
        return "No relevant current events found."
    except requests.exceptions.RequestException as e:
        logging.error(f"NewsAPI error: {str(e)}")
        return "Failed to fetch current events."

def get_industry_trends(category):
    try:
        url = f"https://newsapi.org/v2/everything?q={category}+%22industry+trends%22+%22supply+chain%22+electronics+-optics+-sensor+-battery+-network&sortBy=relevance&apiKey={NEWS_API_KEY}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", [])
        if articles:
            trends = [article["title"] for article in articles[:3]]
            return trends
        return ["No industry trends found."]
    except requests.exceptions.RequestException as e:
        logging.error(f"NewsAPI error for trends: {str(e)}")
        return ["Failed to fetch industry trends."]

def get_tariff_cost(part_number, supplier_data_list):
    tariff_status = "No"  # Default to "No"
    for supplier in supplier_data_list:
        tariff_active = supplier.get("tariff_active", False)
        if tariff_active:
            tariff_status = "Yes"
        logging.debug(f"Supplier: {supplier['name']}, Tariff Active: {tariff_active}")
    return tariff_status  # Return "Yes" or "No" as a string

def get_demand_surge(part_number, current_stock):
    history = PartTrack.query.filter_by(part_num=part_number).order_by(PartTrack.timestamp.desc()).limit(2).all()
    if len(history) < 2:
        return "Unknown", "Insufficient data"
    prev_stock = history[1].stock
    status = "Restocking" if current_stock > prev_stock else "Shortage" if current_stock < prev_stock * 0.5 else "Stable"
    return "Yes" if status == "Shortage" else "No", status

def get_part_info(part_number, custom_url=None):
    result = {
        "part_number": part_number,
        "manufacturer": "Unknown",
        "description": "Not available",
        "category": "Unknown",
        "suppliers": [],
        "availability": "Checking...",
        "single_sourced": "Unknown",
        "high_risk_suppliers": [],
        "cost_alternatives": [],
        "obsolescence_risk": "Unknown",
        "bottlenecks": "None identified",
        "disruption_exposure": "Low",
        "substitution_flexibility": "Unknown",
        "total_landed_cost": "Calculating...",
        "inventory_alignment": "Unknown",
        "sustainability_concerns": "None identified",
        "current_events": "No events found.",
        "risk_score": 0.0,
        "cost_volatility": "Low",
        "industry_trends": [],
        "tariff_cost": "No",
        "demand_surge": "No",
        "demand_status": "No data",
        "eol_approaching": "Unknown"
    }

    distributors = [
        {"name": "DigiKey", "fetch": get_digikey_data},
        {"name": "Mouser", "fetch": get_mouser_data}
    ]

    supplier_data_list = []
    for dist in distributors:
        if dist["name"] == "DigiKey":
            part_data, source_message, product_url, coo, hts, tariff_active = dist["fetch"](part_number)
        else:
            part_data, source_message, product_url, coo, hts, tariff_active, stock = dist["fetch"](part_number)
        
        api_success = part_data is not None and isinstance(part_data, dict)
        
        if api_success:
            if result["manufacturer"] == "Unknown":
                manufacturer = part_data.get("Manufacturer", {}).get("Name") or part_data.get("Manufacturer") or part_data.get("ManufacturerName") or "Unknown"
                result["manufacturer"] = manufacturer

                desc = part_data.get("DetailedDescription", "") or part_data.get("ProductDescription", "") or part_data.get("Description", "")
                if isinstance(desc, dict):
                    desc = desc.get("DetailedDescription", desc.get("ProductDescription", "Not available"))
                result["description"] = desc if desc else "Not available"
                logging.debug(f"Raw description data: {part_data}")

                category = part_data.get("Category", {}).get("Name") or part_data.get("Category", part_data.get("CategoryName"))
                result["category"] = category if category else "Unknown"

                lifecycle_status = part_data.get("ProductStatus", part_data.get("LifecycleStatus", "Unknown"))
                result["obsolescence_risk"] = "Yes" if lifecycle_status in ["EndOfLife", "Discontinued"] else "No"

            supplier_data = {
                "name": dist["name"],
                "availability": "Unknown",
                "lead_time": "Not specified",
                "price_breaks": "Not available",
                "moq": "Unknown",
                "stock": 0,
                "url": product_url
            }
            stock = part_data.get("QuantityAvailable", 0) if dist["name"] == "DigiKey" else stock
            if isinstance(stock, str):
                try:
                    stock = int(stock.split()[0].replace(",", ""))
                except (ValueError, IndexError):
                    stock = 0
            supplier_data["stock"] = stock
            supplier_data["availability"] = f"{stock} In Stock" if stock > 0 else "0 In Stock"
            lead_time = part_data.get("ManufacturerLeadWeeks", 0)
            if isinstance(lead_time, str):
                digits = ''.join(filter(str.isdigit, lead_time))
                lead_time_val = int(digits) if digits else 0
                if "day" in lead_time.lower():
                    lead_time_val = lead_time_val // 7
                lead_time = lead_time_val
            supplier_data["lead_time"] = f"{lead_time} weeks" if lead_time else "Not specified"
            price_breaks = part_data.get("StandardPricing", part_data.get("PriceBreaks", []))
            if not price_breaks and "UnitPrice" in part_data:
                unit_price = part_data.get("UnitPrice", 0)
                price_breaks = [{"Quantity": 1, "Price": str(unit_price)}] if unit_price else []
            if price_breaks:
                supplier_data["price_breaks"] = ", ".join(
                    f"{pb.get('breakQuantity', pb.get('Quantity', 0))}: ${float(pb.get('unitPrice', pb.get('Price', '0').replace('$', '').replace(',', ''))):.2f}"
                    for pb in price_breaks
                )
                supplier_data["moq"] = str(min(pb.get("breakQuantity", pb.get("Quantity", 0)) for pb in price_breaks))
            supplier_data["score"] = calculate_supplier_score(supplier_data, api_success)
            result["suppliers"].append(supplier_data)
            supplier_data_dict = {
                "name": dist["name"],
                "stock": supplier_data["stock"],
                "lead_time": lead_time,
                "region": "US",
                "coo": coo,
                "hts": hts,
                "tariff_active": tariff_active
            }
            if price_breaks:
                supplier_data_dict["min_price"] = float(price_breaks[0]["unitPrice"] if "unitPrice" in price_breaks[0] else price_breaks[0]["Price"].replace('$', '').replace(',', ''))
                supplier_data_list.append(supplier_data_dict)

    supplier_count = len(supplier_data_list)
    result["availability"] = "Yes" if supplier_count > 0 and all(s["stock"] > 0 for s in supplier_data_list) else "No"
    result["single_sourced"] = "Yes" if supplier_count == 1 else "No" if supplier_count > 1 else "Unknown"
    result["high_risk_suppliers"] = [s["name"] for s in supplier_data_list if s["region"] in RISKY_REGIONS]
    result["cost_alternatives"] = sorted(
        [{"supplier": s["name"], "min_price": s["min_price"]} for s in supplier_data_list if "min_price" in s],
        key=lambda x: x["min_price"]
    ) if any("min_price" in s for s in supplier_data_list) else []
    bottleneck_score = sum(1 if s["stock"] < 10 else 0 + 1 if s["lead_time"] > 20 else 0 for s in supplier_data_list) / max(1, supplier_count)
    result["bottlenecks"] = "High" if bottleneck_score > 0.5 else "Low" if bottleneck_score > 0 else "None identified"
    result["disruption_exposure"] = "High" if result["high_risk_suppliers"] else "Low"
    result["total_landed_cost"] = sum(s["min_price"] for s in supplier_data_list if "min_price" in s) + 0.10 * supplier_count if any("min_price" in s for s in supplier_data_list) else "Unknown"
    
    history = PartTrack.query.filter_by(part_num=part_number).order_by(PartTrack.timestamp).all()
    if len(history) >= 2:
        avg_stock = sum(p.stock for p in history) / len(history)
        latest_stock = history[-1].stock
        result["inventory_alignment"] = "Low" if latest_stock < avg_stock * 0.5 else "High" if latest_stock > avg_stock * 1.5 else "Aligned"
        stock_trend = [p.stock for p in history]
        decline_rate = (stock_trend[0] - stock_trend[-1]) / len(stock_trend) if stock_trend[0] > stock_trend[-1] else 0
        result["eol_approaching"] = "Yes" if decline_rate > 10 and result["obsolescence_risk"] == "No" else "No"
        total_stock = sum(s["stock"] for s in supplier_data_list)
        prev_stock = history[-2].stock
        status = "Restocking" if total_stock > prev_stock else "Shortage" if total_stock < prev_stock * 0.5 else "Stable"
        result["demand_surge"] = "Yes" if status == "Shortage" else "No"
        result["demand_status"] = status
    else:
        result["inventory_alignment"] = "Unknown"
        result["eol_approaching"] = "Unknown"
        result["demand_surge"] = "Unknown"
        result["demand_status"] = "Insufficient data"

    subs = Substitutions.query.filter_by(part_num=part_number).all()
    result["substitution_flexibility"] = "Yes" if any(get_part_info(s.substitute_part_num)["suppliers"] for s in subs) else "No"
    result["sustainability_concerns"] = ", ".join(f"{s['name']} low stock" for s in supplier_data_list if s["stock"] < 10) or "None identified"
    result["current_events"] = get_current_events(result["category"], [s["region"] for s in supplier_data_list])
    avg_supplier_score = sum(s["score"] for s in result["suppliers"]) / max(1, supplier_count) if result["suppliers"] else 0
    stock_variance = min((decline_rate / 10), 2) if len(history) >= 2 else 0
    result["risk_score"] = min(max((5 - avg_supplier_score) + (2 if supplier_count == 1 else 0) + stock_variance, 0), 10)
    prices = [p.price for p in history if p.price > 0] + [s["min_price"] for s in supplier_data_list if "min_price" in s]
    result["cost_volatility"] = "High" if prices and (max(prices) - min(prices)) / (sum(prices) / len(prices)) > 0.5 else "Low"
    result["industry_trends"] = get_industry_trends(result["category"])
    result["tariff_cost"] = get_tariff_cost(part_number, supplier_data_list)

    if supplier_data_list:
        for s in supplier_data_list:
            price = s.get("min_price", 0)
            for supplier in result["suppliers"]:
                if supplier["name"] == s["name"] and supplier["price_breaks"] != "Not available":
                    try:
                        price = float(supplier["price_breaks"].split(",")[0].split("$")[1])
                    except (IndexError, ValueError):
                        pass
            last = PartTrack.query.filter_by(part_num=part_number).order_by(PartTrack.timestamp.desc()).first()
            if last and s["stock"] > last.stock:
                db.session.add(SupplierResponse(supplier=s["name"], part_num=part_number, response_time=1.0))
            db.session.add(PartTrack(part_num=part_number, price=price, stock=s["stock"], timestamp="2025-03-30", risk_score=result["risk_score"]))
        db.session.commit()

    return result

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        part_number = request.form["part_number"]
        result = get_part_info(part_number)
        comments = Comment.query.filter_by(part_num=part_number).all()
        return render_template("result.html", data=result, comments=comments)
    return render_template("index.html")

@app.route("/bom", methods=["GET", "POST"])
def bom_upload():
    if request.method == "POST":
        if "bom_file" in request.files:
            file = request.files["bom_file"]
            if not file:
                logging.error("No file uploaded")
                return "No file uploaded", 400
            
            csv_content = file.read().decode("utf-8").splitlines()
            logging.debug(f"Uploaded CSV content: {csv_content}")
            
            parts = []
            reader = csv.DictReader(csv_content)
            possible_part_keys = ["PartNumber", "partnumber", "Part Number", "part_number"]
            part_key = next((key for key in possible_part_keys if key in reader.fieldnames), None)
            
            if not part_key:
                logging.error(f"CSV headers: {reader.fieldnames}. No valid part number column found.")
                return "CSV must have a part number column (e.g., 'PartNumber', 'part_number')", 400
            
            for row in reader:
                part_num = row[part_key]
                logging.debug(f"Processing part: {part_num}")
                part_info = get_part_info(part_num)
                part_info["is_critical"] = bool(FavoritePart.query.filter_by(part_num=part_num, critical=True).first())
                parts.append(part_info)
            
            session['bom_parts'] = parts
            logging.info(f"Saved {len(parts)} parts to session['bom_parts']")
            resilience_score = sum(p["risk_score"] for p in parts if "risk_score" in p) / len(parts) if parts else 0
            resilience_score = 100 - (resilience_score * 10)
            logging.debug(f"Initial resilience_score: {resilience_score}")
            return render_template("bom.html", parts=parts, resilience_score=resilience_score)
        
        parts = session.get('bom_parts', [])
        logging.info(f"Retrieved {len(parts)} parts from session['bom_parts']")
        if not parts:
            logging.warning("No parts found in session - redirecting to upload")
            return redirect(url_for('bom_upload'))
        
        resilience_score = sum(p["risk_score"] for p in parts if "risk_score" in p) / len(parts) if parts else 0
        resilience_score = 100 - (resilience_score * 10)
        logging.debug(f"Resilience_score after retrieval: {resilience_score}")

        if "save_favorites" in request.form or "tag_critical" in request.form:
            selected_parts = request.form.getlist("favorite_parts")
            critical_parts = request.form.getlist("critical_parts")
            for part_num in selected_parts:
                if not FavoritePart.query.filter_by(part_num=part_num).first():
                    db.session.add(FavoritePart(part_num=part_num, critical=part_num in critical_parts))
            for part_num in critical_parts:
                fav = FavoritePart.query.filter_by(part_num=part_num).first()
                if fav:
                    fav.critical = True
            db.session.commit()
            for part in parts:
                part["is_critical"] = part["part_number"] in critical_parts
                part["is_favorite"] = part["part_number"] in selected_parts
            session['bom_parts'] = parts
            logging.info("Updated favorites/critical tags in session")
            return render_template("bom.html", parts=parts, resilience_score=resilience_score)
        
        if "download_report" in request.form:
            report_data = {}
            for part in parts:
                part_num = part["part_number"]
                if part_num not in report_data:
                    report_data[part_num] = {
                        "manufacturer": part["manufacturer"],
                        "total_stock": 0,
                        "best_price": 0.0,
                        "suppliers": [],
                        "risk_score": part["risk_score"]
                    }
                report_part = report_data[part_num]
                report_part["total_stock"] += sum(s["stock"] for s in part["suppliers"])
                for supplier in part["suppliers"]:
                    price = 0.0
                    if supplier["price_breaks"] != "Not available":
                        try:
                            price = float(supplier["price_breaks"].split(",")[0].split("$")[1])
                        except (IndexError, ValueError):
                            pass
                    report_part["best_price"] = min(report_part["best_price"], price) if report_part["best_price"] else price
                    report_part["suppliers"].append({
                        "name": supplier["name"],
                        "stock": supplier["stock"],
                        "price": price,
                        "lead_time": supplier["lead_time"]
                    })

            buffer = BytesIO()
            c = canvas.Canvas(buffer, pagesize=letter)
            c.drawString(100, 750, "BOM Report - March 30, 2025")
            y = 700
            for part_num, data in report_data.items():
                c.drawString(100, y, f"{part_num} ({data['manufacturer']})")
                y -= 20
                c.drawString(110, y, f"Total Stock: {data['total_stock']}")
                y -= 20
                c.drawString(110, y, f"Best Price: ${data['best_price']:.2f}")
                y -= 20
                c.drawString(110, y, f"Risk Score: {data['risk_score']:.1f}/10")
                y -= 20
                for supplier in data["suppliers"]:
                    c.drawString(120, y, f"{supplier['name']}: Stock: {supplier['stock']}, Price: ${supplier['price']:.2f}, Lead: {supplier['lead_time']}")
                    y -= 20
                y -= 10
                if y < 50:
                    c.showPage()
                    y = 750
            c.save()
            buffer.seek(0)
            logging.info("Generated BOM report")
            return send_file(buffer, as_attachment=True, download_name="bom_report.pdf")

        return render_template("bom.html", parts=parts, resilience_score=resilience_score)

    return render_template("bom_upload.html")

@app.route("/bom_template")
def bom_template():
    csv_data = StringIO()
    writer = csv.writer(csv_data)
    writer.writerow(["PartNumber"])
    writer.writerow(["640456-5"])
    writer.writerow(["LM317"])
    writer.writerow(["NE555"])
    return Response(
        csv_data.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=bom_template.csv"}
    )

@app.route("/comment/<part_number>", methods=["POST"])
def add_comment(part_number):
    comment_text = request.form["comment"]
    db.session.add(Comment(part_num=part_number, comment=comment_text))
    db.session.commit()
    return redirect(url_for("index"))

@app.route("/save_favorite/<part_number>", methods=["POST"])
def save_favorite(part_number):
    if not FavoritePart.query.filter_by(part_num=part_number).first():
        db.session.add(FavoritePart(part_num=part_number))
    db.session.commit()
    return redirect(url_for("index"))

@app.route("/favorites", methods=["GET", "POST"])
def favorites():
    favorite_parts = FavoritePart.query.all()
    parts_data = [get_part_info(part.part_num) for part in favorite_parts]
    
    if request.method == "POST" and ("save_favorites" in request.form or "tag_critical" in request.form):
        selected_parts = request.form.getlist("favorite_parts")
        critical_parts = request.form.getlist("critical_parts")
        for part_num in selected_parts:
            if not FavoritePart.query.filter_by(part_num=part_num).first():
                db.session.add(FavoritePart(part_num=part_num, critical=part_num in critical_parts))
        for part_num in critical_parts:
            fav = FavoritePart.query.filter_by(part_num=part_num).first()
            if fav:
                fav.critical = True
        db.session.commit()
        return redirect(url_for("favorites"))
    
    return render_template("favorites.html", parts=parts_data)

@app.route("/bulk", methods=["POST"])
def bulk_lookup():
    part_numbers = request.form["part_numbers"].split(",")
    parts_data = [get_part_info(p.strip()) for p in part_numbers]
    return render_template("bulk.html", parts=parts_data)

if __name__ == "__main__":
    app.run(debug=True)