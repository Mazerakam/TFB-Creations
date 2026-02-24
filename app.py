from flask import Flask, request, jsonify
from flask_cors import CORS
import trimesh
import numpy as np
import tempfile
import os
import requests

app = Flask(__name__)
CORS(app)

@app.after_request
def after_request(response):
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type")
    response.headers.add("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return response

# ===================== CONFIG PRIX =====================
PRIX_KG = {"PLA": 25.0, "PETG": 28.0, "TPU": 35.0}
DENSITE = {"PLA": 1.24, "PETG": 1.27, "TPU": 1.20}
REMPLISSAGE      = 0.30
EPAISSEUR_COQUE  = 0.12
COEFFICIENT_MARGE = 3.5
FORFAIT_MACHINE  = 2.0
PRIX_MIN         = 5.0

# ===================== CONFIG SHOPIFY =====================
SHOPIFY_STORE    = "tf-b-creations.myshopify.com"
SHOPIFY_TOKEN    = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")  # Variable d'env sur Render

# ===================== ROUTES =====================

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "service": "TechFix & Build — STL Analyzer API"})


@app.route("/analyze", methods=["POST", "OPTIONS"])
def analyze():
    if request.method == "OPTIONS":
        return "", 200

    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier reçu"}), 400

    f = request.files["file"]
    materiau = request.form.get("materiau", "PLA").upper()
    echelle  = float(request.form.get("echelle", 1.0))

    if materiau not in DENSITE:
        return jsonify({"error": f"Matériau inconnu : {materiau}"}), 400

    suffix = os.path.splitext(f.filename)[1].lower()
    if suffix not in [".stl", ".3mf"]:
        return jsonify({"error": "Format non supporté. Utilisez .stl ou .3mf"}), 400

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    try:
        mesh = trimesh.load(tmp_path, force="mesh")
        if not mesh.is_watertight:
            trimesh.repair.fix_normals(mesh)
            trimesh.repair.fill_holes(mesh)

        volume_mm3  = abs(mesh.volume)
        volume_cm3  = volume_mm3 / 1000.0 * (echelle ** 3)
        surface_cm2 = mesh.area / 100.0
        volume_coque   = surface_cm2 * EPAISSEUR_COQUE
        volume_imprime = (volume_cm3 * REMPLISSAGE) + volume_coque
        poids_g        = volume_imprime * DENSITE[materiau]
        prix_filament  = (poids_g / 1000.0) * PRIX_KG[materiau]
        prix_final     = max((prix_filament * COEFFICIENT_MARGE) + FORFAIT_MACHINE, PRIX_MIN)

        bounds  = mesh.bounds
        dims_mm = ((bounds[1] - bounds[0]) * echelle).tolist()

        return jsonify({
            "volume_cm3":         round(volume_cm3, 2),
            "volume_imprime_cm3": round(volume_imprime, 2),
            "poids_g":            round(poids_g, 1),
            "materiau":           materiau,
            "prix_filament_eur":  round(prix_filament, 2),
            "prix_final_eur":     round(prix_final, 2),
            "dimensions_mm": {
                "largeur":    round(dims_mm[0], 1),
                "profondeur": round(dims_mm[1], 1),
                "hauteur":    round(dims_mm[2], 1),
            },
            "watertight": mesh.is_watertight,
        })

    except Exception as e:
        return jsonify({"error": f"Erreur analyse : {str(e)}"}), 500
    finally:
        os.unlink(tmp_path)


@app.route("/create-order", methods=["POST", "OPTIONS"])
def create_order():
    if request.method == "OPTIONS":
        return "", 200

    if not SHOPIFY_TOKEN:
        return jsonify({"error": "Token Shopify non configuré sur le serveur"}), 500

    data = request.get_json()
    if not data:
        return jsonify({"error": "Données manquantes"}), 400

    # Champs attendus
    fichier    = data.get("fichier", "—")
    lien       = data.get("lien", "—")
    materiau   = data.get("materiau", "PLA")
    couleur    = data.get("couleur", "—")
    dimensions = data.get("dimensions", "—")
    prix       = float(data.get("prix", 5.0))
    email      = data.get("email", "")

    # Construire le Draft Order Shopify
    note_lines = [
        f"📄 Fichier : {fichier}",
        f"📎 Lien fichier : {lien}",
        f"🧱 Matériau : {materiau}",
        f"🎨 Couleur : {couleur}",
        f"📐 Dimensions : {dimensions}",
    ]

    draft_order = {
        "draft_order": {
            "line_items": [{
                "title": "Impression 3D sur mesure",
                "price": str(round(prix, 2)),
                "quantity": 1,
                "requires_shipping": True,
                "properties": [
                    {"name": "📄 Fichier",      "value": fichier},
                    {"name": "📎 Lien fichier", "value": lien},
                    {"name": "🧱 Matériau",     "value": materiau},
                    {"name": "🎨 Couleur",      "value": couleur},
                    {"name": "📐 Dimensions",   "value": dimensions},
                ]
            }],
            "note": "\n".join(note_lines),
            "tags": "impression-3d-custom",
            "use_customer_default_address": False,
        }
    }

    if email:
        draft_order["draft_order"]["email"] = email

    # Appel Admin API Shopify
    url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/draft_orders.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, json=draft_order, headers=headers, timeout=15)
        resp_data = resp.json()

        if resp.status_code != 201:
            return jsonify({
                "error": "Erreur Shopify",
                "details": resp_data
            }), resp.status_code

        invoice_url = resp_data["draft_order"].get("invoice_url", "")
        order_id    = resp_data["draft_order"].get("id", "")
        order_name  = resp_data["draft_order"].get("name", "")

        # Envoyer la facture par email si fourni
        if email and invoice_url:
            invoice_url_api = f"https://{SHOPIFY_STORE}/admin/api/2024-01/draft_orders/{order_id}/send_invoice.json"
            requests.post(invoice_url_api, json={
                "draft_order_invoice": {
                    "to": email,
                    "subject": f"Votre devis impression 3D — {order_name}",
                    "custom_message": f"Bonjour,\n\nVoici votre lien de paiement pour votre impression 3D sur mesure ({dimensions}, {materiau}, {couleur}).\n\nMerci de votre confiance !\nTechFix & Build"
                }
            }, headers=headers, timeout=10)

        return jsonify({
            "success": True,
            "invoice_url": invoice_url,
            "order_name": order_name,
        })

    except Exception as e:
        return jsonify({"error": f"Erreur réseau : {str(e)}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
