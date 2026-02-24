from flask import Flask, request, jsonify
from flask_cors import CORS
import trimesh
import numpy as np
import tempfile
import os
import urllib.request
import urllib.error
import json as json_lib

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
REMPLISSAGE       = 0.30
EPAISSEUR_COQUE   = 0.12
COEFFICIENT_MARGE = 3.5
FORFAIT_MACHINE   = 2.0
PRIX_MIN          = 5.0

# ===================== CONFIG SHOPIFY =====================
SHOPIFY_STORE = "tf-b-creations.myshopify.com"
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")

# ===================== ROUTES =====================

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "service": "TechFix & Build — STL Analyzer API"})


@app.route("/analyze", methods=["POST", "OPTIONS"])
def analyze():
    if request.method == "OPTIONS":
        return "", 200

    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier recu"}), 400

    f = request.files["file"]
    materiau = request.form.get("materiau", "PLA").upper()
    echelle  = float(request.form.get("echelle", 1.0))

    if materiau not in DENSITE:
        return jsonify({"error": f"Materiau inconnu : {materiau}"}), 400

    suffix = os.path.splitext(f.filename)[1].lower()
    if suffix not in [".stl", ".3mf"]:
        return jsonify({"error": "Format non supporte. Utilisez .stl ou .3mf"}), 400

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    try:
        mesh = trimesh.load(tmp_path, force="mesh")
        if not mesh.is_watertight:
            trimesh.repair.fix_normals(mesh)
            trimesh.repair.fill_holes(mesh)

        volume_mm3     = abs(mesh.volume)
        volume_cm3     = volume_mm3 / 1000.0 * (echelle ** 3)
        surface_cm2    = mesh.area / 100.0
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
        return jsonify({"error": "Token Shopify non configure sur le serveur"}), 500

    data = request.get_json()
    if not data:
        return jsonify({"error": "Donnees manquantes"}), 400

    fichier    = data.get("fichier", "-")
    lien       = data.get("lien", "-")
    materiau   = data.get("materiau", "PLA")
    couleur    = data.get("couleur", "-")
    plaque     = data.get("plaque", "Aucune (lisse)")
    dimensions = data.get("dimensions", "-")
    prix       = float(data.get("prix", 5.0))

    draft_order = {
        "draft_order": {
            "line_items": [{
                "title": "Impression 3D sur mesure",
                "price": str(round(prix, 2)),
                "quantity": 1,
                "requires_shipping": True,
                "properties": [
                    {"name": "Fichier",      "value": fichier},
                    {"name": "Lien fichier", "value": lien},
                    {"name": "Materiau",     "value": materiau},
                    {"name": "Couleur",      "value": couleur},
                    {"name": "Plaque",       "value": plaque},
                    {"name": "Dimensions",   "value": dimensions},
                ]
            }],
            "note": f"Fichier: {fichier}\nLien: {lien}\nMateriau: {materiau}\nCouleur: {couleur}\nPlaque: {plaque}\nDimensions: {dimensions}",
            "tags": "impression-3d-custom",
        }
    }

    url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/draft_orders.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json",
    }

    try:
        body = json_lib.dumps(draft_order).encode("utf-8")
        req  = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            resp_data = json_lib.loads(r.read().decode())

        invoice_url = resp_data["draft_order"].get("invoice_url", "")
        order_name  = resp_data["draft_order"].get("name", "")

        return jsonify({
            "success":     True,
            "invoice_url": invoice_url,
            "order_name":  order_name,
        })

    except urllib.error.HTTPError as e:
        return jsonify({"error": "Erreur Shopify", "details": e.read().decode()}), e.code
    except Exception as e:
        return jsonify({"error": f"Erreur reseau : {str(e)}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
