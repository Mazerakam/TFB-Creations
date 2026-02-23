from flask import Flask, request, jsonify
from flask_cors import CORS
import trimesh
import numpy as np
import tempfile
import os

app = Flask(__name__)
CORS(app)  # Autorise les requêtes depuis Shopify

# ===================== CONFIG PRIX =====================
PRIX_KG = {
    "PLA":  25.0,   # € par kg — à modifier selon tes prix réels
    "PETG": 28.0,
    "TPU":  35.0,
}

DENSITE = {
    "PLA":  1.24,   # g/cm³
    "PETG": 1.27,
    "TPU":  1.20,
}

REMPLISSAGE      = 0.30   # 30% infill
EPAISSEUR_COQUE  = 0.12   # cm (= 1.2mm, 3 parois standard)
COEFFICIENT_MARGE = 3.5   # ×3.5 sur le coût matière
FORFAIT_MACHINE  = 2.0    # € forfait électricité + usure
PRIX_MIN         = 5.0    # Prix minimum absolu en €

# ===================== ROUTES =====================

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "service": "TechFix & Build — STL Analyzer API"})


@app.route("/analyze", methods=["POST"])
def analyze():
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier reçu"}), 400

    f = request.files["file"]
    materiau = request.form.get("materiau", "PLA").upper()
    echelle  = float(request.form.get("echelle", 1.0))  # facteur d'échelle si client redimensionne

    if materiau not in DENSITE:
        return jsonify({"error": f"Matériau inconnu : {materiau}. Valeurs acceptées : PLA, PETG, TPU"}), 400

    # Sauvegarde temporaire du fichier
    suffix = os.path.splitext(f.filename)[1].lower()
    if suffix not in [".stl", ".3mf"]:
        return jsonify({"error": "Format non supporté. Utilisez .stl ou .3mf"}), 400

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    try:
        mesh = trimesh.load(tmp_path, force="mesh")

        if not mesh.is_watertight:
            # On tente une réparation automatique
            trimesh.repair.fix_normals(mesh)
            trimesh.repair.fill_holes(mesh)

        # Volume en cm³ (trimesh retourne en mm³ si unités non définies → on divise par 1000)
        volume_mm3 = abs(mesh.volume)
        volume_cm3 = volume_mm3 / 1000.0

        # Application de l'échelle (volume scale cubique)
        volume_cm3 *= (echelle ** 3)

        # Surface en cm² pour estimation coque
        surface_cm2 = mesh.area / 100.0

        # Calcul volume imprimé
        volume_coque   = surface_cm2 * EPAISSEUR_COQUE
        volume_imprime = (volume_cm3 * REMPLISSAGE) + volume_coque

        # Poids
        poids_g = volume_imprime * DENSITE[materiau]

        # Coût matière
        prix_filament = (poids_g / 1000.0) * PRIX_KG[materiau]

        # Prix final
        prix_final = max(
            (prix_filament * COEFFICIENT_MARGE) + FORFAIT_MACHINE,
            PRIX_MIN
        )

        # Dimensions bounding box
        bounds = mesh.bounds  # [[xmin,ymin,zmin],[xmax,ymax,zmax]]
        dims_mm = (bounds[1] - bounds[0]) * echelle
        dims_mm = dims_mm.tolist()

        return jsonify({
            "volume_cm3":     round(volume_cm3, 2),
            "volume_imprime_cm3": round(volume_imprime, 2),
            "poids_g":        round(poids_g, 1),
            "materiau":       materiau,
            "prix_filament_eur": round(prix_filament, 2),
            "prix_final_eur": round(prix_final, 2),
            "dimensions_mm":  {
                "largeur": round(dims_mm[0], 1),
                "profondeur": round(dims_mm[1], 1),
                "hauteur": round(dims_mm[2], 1),
            },
            "watertight": mesh.is_watertight,
        })

    except Exception as e:
        return jsonify({"error": f"Erreur analyse : {str(e)}"}), 500

    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
