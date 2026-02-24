from flask import Flask, request, jsonify
from flask_cors import CORS
import trimesh
import numpy as np
import tempfile
import os
import uuid
import urllib.request
import urllib.error
import json as json_lib
from datetime import datetime

app = Flask(__name__)
CORS(app)



@app.after_request
def after_request(response):
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type")
    response.headers.add("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return response

# ===================== CONFIG PRIX =====================
PRIX_KG = {"PLA": 25.0, "PETG": 28.0, "TPU": 35.0, "ASA": 28.0}
DENSITE = {"PLA": 1.24, "PETG": 1.27, "TPU": 1.20, "ASA": 1.07}

# Vitesse d'extrusion estimée par matériau (mm³/s) — sert au calcul du temps
VITESSE_MM3_S = {"PLA": 8.0, "PETG": 6.5, "TPU": 3.5, "ASA": 6.0}

REMPLISSAGE       = 0.20   # 20% infill
EPAISSEUR_COQUE   = 0.12
PRIX_HEURE_MACHINE = 1.50  # €/heure d'impression
COEFFICIENT_MARGE  = 1.40  # 40% de marge
PRIX_MIN           = 3.0   # plancher absolu

# Plateau Anycubic Kobra MAX
PLATEAU = {"x": 250.0, "y": 250.0, "z": 250.0}

# ===================== CONFIG SHOPIFY =====================
SHOPIFY_STORE = "tf-b-creations.myshopify.com"
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")

# ===================== CONFIG CLOUDFLARE R2 =====================
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET     = os.environ.get("R2_BUCKET", "tfb-stl-files")

def get_r2_client():
    """Retourne un client boto3 compatible R2. Retourne None si non configuré."""
    if not all([R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY]):
        return None
    try:
        import boto3
        return boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name="auto"
        )
    except ImportError:
        print("boto3 non installé — upload R2 désactivé")
        return None

def upload_stl_r2(file_bytes, original_filename):
    """Upload un fichier STL vers R2. Retourne la clé ou None si échec."""
    client = get_r2_client()
    if not client:
        return None
    try:
        date_str  = datetime.now().strftime("%Y%m%d")
        unique_id = str(uuid.uuid4())[:8]
        key       = f"{date_str}/{unique_id}_{original_filename}"
        client.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=file_bytes,
            ContentType="application/octet-stream",
            Metadata={"original_name": original_filename}
        )
        return key
    except Exception as e:
        print(f"R2 upload failed: {e}")
        return None

# ===================== CALCUL PRIX =====================

def calculer_prix(mesh, materiau, echelle=1.0):
    """
    Calcule le prix en croisant :
    - poids (matière × infill)
    - temps d'impression estimé (volume / vitesse extrusion)
    - occupation du plateau (supplément si >60%)
    - warnings si hors plateau
    """
    densite    = DENSITE.get(materiau, 1.24)
    vitesse    = VITESSE_MM3_S.get(materiau, 8.0)
    prix_kg    = PRIX_KG.get(materiau, 25.0)

    # — Dimensions & vérification plateau —
    bounds  = mesh.bounds
    dims_mm = (bounds[1] - bounds[0]) * echelle
    warnings = []
    for axe, val, maxi in zip(["X", "Y", "Z"], dims_mm, [PLATEAU["x"], PLATEAU["y"], PLATEAU["z"]]):
        if val > maxi:
            warnings.append(f"Dimension {axe} ({val:.0f} mm) dépasse le plateau max ({int(maxi)} mm)")

    # — Volume —
    volume_mm3     = abs(mesh.volume) * (echelle ** 3)
    volume_cm3     = volume_mm3 / 1000.0
    surface_cm2    = (mesh.area * (echelle ** 2)) / 100.0
    volume_coque   = surface_cm2 * EPAISSEUR_COQUE
    volume_imprime = (volume_cm3 * REMPLISSAGE) + volume_coque  # cm³ de matière réelle

    # — Poids —
    poids_g = volume_imprime * densite

    # — Temps d'impression (volume mm³ de matière / vitesse mm³/s) —
    volume_mat_mm3  = volume_imprime * 1000.0   # cm³ → mm³
    temps_secondes  = volume_mat_mm3 / vitesse
    temps_heures    = temps_secondes / 3600.0
    temps_label     = f"{int(temps_heures)}h{int((temps_heures % 1) * 60):02d}"

    # — Surface plateau (empreinte XY) —
    surface_xy      = dims_mm[0] * dims_mm[1]
    surface_max     = PLATEAU["x"] * PLATEAU["y"]
    ratio_plateau   = surface_xy / surface_max
    suppl_plateau   = 0.50 if ratio_plateau > 0.60 else 0.0

    # — Coûts —
    cout_matiere = (poids_g / 1000.0) * prix_kg
    cout_machine = temps_heures * PRIX_HEURE_MACHINE

    # — Prix final —
    prix_brut  = (cout_matiere + cout_machine + suppl_plateau) * COEFFICIENT_MARGE
    prix_final = max(round(prix_brut, 2), PRIX_MIN)

    return {
        "prix_final_eur":       prix_final,
        "prix_filament_eur":    round(cout_matiere, 2),
        "poids_g":              round(poids_g, 1),
        "volume_cm3":           round(volume_cm3, 2),
        "volume_imprime_cm3":   round(volume_imprime, 2),
        "temps_impression":     temps_label,
        "surface_plateau_pct":  round(ratio_plateau * 100, 1),
        "materiau":             materiau,
        "dimensions_mm": {
            "largeur":    round(dims_mm[0], 1),
            "profondeur": round(dims_mm[1], 1),
            "hauteur":    round(dims_mm[2], 1),
        },
        "warnings": warnings,
    }

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

    f        = request.files["file"]
    materiau = request.form.get("materiau", "PLA").upper()
    echelle  = float(request.form.get("echelle", 1.0))

    if materiau not in DENSITE:
        return jsonify({"error": f"Materiau inconnu : {materiau}"}), 400

    suffix = os.path.splitext(f.filename)[1].lower()
    if suffix not in [".stl", ".3mf"]:
        return jsonify({"error": "Format non supporte. Utilisez .stl ou .3mf"}), 400

    # Lire les bytes une seule fois (pour upload R2 + analyse)
    file_bytes = f.read()

    # Upload R2 en arrière-plan (non bloquant si échec)
    r2_key = upload_stl_r2(file_bytes, f.filename)

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        mesh = trimesh.load(tmp_path, force="mesh")
        if not mesh.is_watertight:
            trimesh.repair.fix_normals(mesh)
            trimesh.repair.fill_holes(mesh)

        result = calculer_prix(mesh, materiau, echelle)
        result["watertight"] = mesh.is_watertight
        result["r2_key"]     = r2_key  # None si R2 non configuré

        return jsonify(result)

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

    fichier           = data.get("fichier", "-")
    lien              = data.get("lien", "-")
    materiau          = data.get("materiau", "PLA")
    couleur           = data.get("couleur", "-")
    plaque            = data.get("plaque", "Aucune (lisse)")
    finition          = data.get("finition", "Impression brute")
    dimensions        = data.get("dimensions", "-")
    temps_impression  = data.get("temps_impression", "-")
    surface_plateau   = data.get("surface_plateau_pct", "-")
    commentaire       = data.get("commentaire", "")
    r2_key            = data.get("r2_key", "-")
    prix              = float(data.get("prix", 3.0))

    note = "\n".join([
        f"Fichier       : {fichier}",
        f"Lien STL      : {lien}",
        f"Fichier R2    : {r2_key}",
        f"Matériau      : {materiau}",
        f"Couleur       : {couleur}",
        f"Plaque        : {plaque}",
        f"Finition      : {finition}",
        f"Dimensions    : {dimensions}",
        f"Temps estimé  : {temps_impression}",
        f"Surface plat. : {surface_plateau}%",
        *([ f"Commentaire   : {commentaire}" ] if commentaire else []),
    ])

    draft_order = {
        "draft_order": {
            "line_items": [{
                "title": f"Impression 3D sur mesure — {materiau}",
                "price": str(round(prix, 2)),
                "quantity": 1,
                "requires_shipping": True,
                "properties": [
                    {"name": "Fichier",          "value": fichier},
                    {"name": "Lien fichier",      "value": lien},
                    {"name": "Matériau",          "value": materiau},
                    {"name": "Couleur",           "value": couleur},
                    {"name": "Plaque",            "value": plaque},
                    {"name": "Finition",          "value": finition},
                    {"name": "Dimensions",        "value": dimensions},
                    {"name": "Temps impression",  "value": temps_impression},
                    {"name": "Surface plateau",   "value": f"{surface_plateau}%"},
                    {"name": "Fichier R2",        "value": r2_key or "-"},
                    *([ {"name": "Commentaire", "value": commentaire} ] if commentaire else []),
                ]
            }],
            "note": note,
            "tags": "impression-3d-custom,a-valider",
            # Pas de send_receipt ni complete → reste en brouillon à valider manuellement
        }
    }

    url     = f"https://{SHOPIFY_STORE}/admin/api/2024-01/draft_orders.json"
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
