"""
GPS Audio Server
Recibe coordenadas GPS → Geoapify (reverse geocoding + places) → TTS → WAV

Casos de respuesta:
  1a. Esquina X e Y                          (en esquina, sin lugar cercano)
  1b. Calle N                                (solo dirección)
  2.  Calle N, próximo a Y                   (cerca de esquina, sin lugar)
  3.  Calle N, <Lugar>                       (en lugar conocido)
  4.  Calle N, próximo al <Lugar>            (cerca de lugar, sin esquina próxima)
  5.  <Lugar>                                (sin calle cercana, hay lugar)
  6.  <Calle> a X metros  [fallback]         (sin calle cercana ni lugar)
"""

import io
import os
import math
import logging
import tempfile
from flask import Flask, request, send_file, jsonify
import requests
from gtts import gTTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

GEOAPIFY_API_KEY = os.environ.get("GEOAPIFY_API_KEY", "")
TTS_LANG         = "es"

# ── Umbrales de distancia (metros) ────────────────────────────────────────────

D_CALLE_MAX      = 50   # distancia máxima para considerar calle "cercana"
D_ESQUINA        = 15   # distancia para "estar en la esquina"
D_ESQUINA_PROX   = 30   # distancia para "próximo a" esquina
D_LUGAR          = 15   # distancia para "estar en" el lugar
D_LUGAR_PROX     = 50   # distancia para "próximo a" lugar

# Categorías de lugares conocidos relevantes para orientación.
# Se usan categorías PADRE (sin subcategoría) para mayor compatibilidad:
# la API devuelve todos sus hijos automáticamente.
# Verificadas contra documentación oficial de Geoapify Places API.
PLACE_CATEGORIES = ",".join([
    "religion",           # iglesias, templos, mezquitas
    "education",          # escuelas, universidades, colegios
    "healthcare",         # hospitales, farmacias, centros de salud
    "public_transport",   # paradas de colectivo, estaciones, metro
    "leisure.park",       # parques y plazas
    "tourism",            # museos, monumentos, atracciones, info turística
    "office.government",  # municipalidad, correo, organismos públicos
    "service",            # policía, bomberos, servicios de emergencia
    "commercial.supermarket",  # supermercados
    "sport.stadium",      # estadios
])

# ── Utilidades ────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    """Distancia en metros entre dos coordenadas."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Llamadas a Geoapify ───────────────────────────────────────────────────────

def _get(url, params, label):
    params["apiKey"] = GEOAPIFY_API_KEY
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    log.info("%s → %d features", label, len(data.get("features", [])))
    return data.get("features", [])


def fetch_reverse_geocode(lat, lon):
    """
    Devuelve el feature más cercano del reverse geocoding.
    Incluye campo sintético 'distance_m' calculado desde (lat, lon).
    """
    features = _get(
        "https://api.geoapify.com/v1/geocode/reverse",
        {"lat": lat, "lon": lon, "lang": "es"},
        "reverse_geocode",
    )
    if not features:
        return None
    feat = features[0]
    props = feat["properties"]
    # Calcular distancia real al feature
    feat_lat = props.get("lat") or feat["geometry"]["coordinates"][1]
    feat_lon = props.get("lon") or feat["geometry"]["coordinates"][0]
    props["_dist_m"] = haversine(lat, lon, feat_lat, feat_lon)
    return props


def fetch_nearby_streets(lat, lon):
    """
    Detecta calles transversales desplazando el punto en 4 direcciones
    cardinales y comparando los nombres que devuelve cada reverse geocoding.

    Por qué este enfoque: el reverse geocoding siempre devuelve el segmento
    más cercano al punto dado. Para encontrar una calle transversal hay que
    "mirar" lateralmente: si al desplazarse hacia el norte/sur/este/oeste
    el nombre de calle cambia, es porque hay una intersección en esa dirección.
    """
    main = fetch_reverse_geocode(lat, lon)
    calle_principal = (main.get("street") or "").lower() if main else ""

    # offset equivale a D_ESQUINA_PROX metros en grados (~111km por grado)
    offset = D_ESQUINA_PROX / 111_000

    puntos = [
        (lat + offset, lon),   # norte
        (lat - offset, lon),   # sur
        (lat, lon + offset),   # este
        (lat, lon - offset),   # oeste
    ]

    vistas = {}  # nombre -> distancia mínima al punto original
    for p_lat, p_lon in puntos:
        features = _get(
            "https://api.geoapify.com/v1/geocode/reverse",
            {"lat": p_lat, "lon": p_lon, "lang": "es"},
            "street_probe",
        )
        if not features:
            continue
        props = features[0]["properties"]
        nombre = props.get("street") or props.get("name") or ""
        if not nombre or nombre.lower() == calle_principal:
            continue
        dist = haversine(lat, lon, p_lat, p_lon)
        if nombre not in vistas or vistas[nombre] > dist:
            vistas[nombre] = dist

    return [{"name": n, "dist_m": d} for n, d in vistas.items()]


def fetch_nearby_places(lat, lon, radius=D_LUGAR_PROX + 20):
    """
    Busca lugares conocidos cercanos usando la Places API.
    Usa el campo 'distance' nativo de Geoapify cuando está disponible;
    es más preciso que haversine al centroide porque Geoapify lo calcula
    al borde del polígono del lugar.
    """
    features = _get(
        "https://api.geoapify.com/v2/places",
        {
            "categories": PLACE_CATEGORIES,
            "filter": f"circle:{lon},{lat},{radius}",
            "bias": f"proximity:{lon},{lat}",
            "limit": 5,
            "lang": "es",
        },
        "nearby_places",
    )
    places = []
    for feat in features:
        props = feat["properties"]
        name = props.get("name")
        if not name:
            continue
        # Preferir 'distance' nativo; fallback a haversine al centroide
        dist = props.get("distance")
        if dist is None:
            feat_lat = props.get("lat") or feat["geometry"]["coordinates"][1]
            feat_lon = props.get("lon") or feat["geometry"]["coordinates"][0]
            dist = haversine(lat, lon, feat_lat, feat_lon)
        places.append({
            "name": name,
            "categories": props.get("categories", []),
            "dist_m": dist,
        })
    places.sort(key=lambda p: p["dist_m"])
    return places


# ── Construcción de la dirección base ─────────────────────────────────────────

def build_base_address(props):
    """Construye 'Calle 123' o 'Calle' a partir de las propiedades del geocoding."""
    street      = props.get("street")
    housenumber = props.get("housenumber")
    if street and housenumber:
        return f"{street} {housenumber}"
    if street:
        return street
    # Fallback: formatted
    return props.get("formatted", "Dirección desconocida")


def build_fallback_address(props):
    """Para el caso 6: calle con distancia."""
    name = (
        props.get("street")
        or props.get("road")
        or props.get("name")
        or props.get("formatted", "vía desconocida")
    )
    dist = props.get("_dist_m", 0)
    return f"{name} a {int(round(dist))} metros"


# ── Lógica de casos ───────────────────────────────────────────────────────────

def build_location_message(lat, lon):
    """
    Aplica el árbol de decisión y devuelve el texto de ubicación.

    Casos:
      1a. Esquina X e Y
      1b. Calle N
      2.  Calle N, próximo a Y
      3.  Calle N, <Lugar>
      4.  Calle N, próximo al <Lugar>
      5.  <Lugar>
      6.  <Calle> a X metros  [fallback]
    """
    # Obtener datos en paralelo conceptual (secuencial aquí por simplicidad)
    geo_props = fetch_reverse_geocode(lat, lon)
    places    = fetch_nearby_places(lat, lon)

    # ── Distancia a la calle más cercana ──────────────────────────────────────
    dist_calle = geo_props["_dist_m"] if geo_props else float("inf")
    hay_calle  = dist_calle <= D_CALLE_MAX

    # ── Lugar más cercano ─────────────────────────────────────────────────────
    lugar_en   = next((p for p in places if p["dist_m"] <= D_LUGAR),       None)
    lugar_prox = next((p for p in places if p["dist_m"] <= D_LUGAR_PROX),  None)

    # ── Calles transversales (para esquina) ───────────────────────────────────
    streets        = fetch_nearby_streets(lat, lon) if hay_calle else []
    calle_base     = build_base_address(geo_props) if geo_props else None
    # Filtrar la calle principal para encontrar transversales
    calle_principal = geo_props.get("street", "") if geo_props else ""
    transversales  = [
        s for s in streets
        if s["name"].lower() != calle_principal.lower()
    ]
    esquina_en     = next((s for s in transversales if s["dist_m"] <= D_ESQUINA),      None)
    esquina_prox   = next((s for s in transversales if s["dist_m"] <= D_ESQUINA_PROX), None)

    log.info(
        "dist_calle=%.0fm hay_calle=%s lugar_en=%s lugar_prox=%s "
        "esquina_en=%s esquina_prox=%s",
        dist_calle, hay_calle,
        lugar_en["name"]   if lugar_en   else None,
        lugar_prox["name"] if lugar_prox else None,
        esquina_en["name"] if esquina_en else None,
        esquina_prox["name"] if esquina_prox else None,
    )

    # ── Árbol de decisión ─────────────────────────────────────────────────────

    if not hay_calle:
        if lugar_prox:
            # CASO 5: solo lugar
            log.info("CASO 5 – solo lugar")
            return lugar_prox["name"]
        else:
            # CASO 6: fallback con distancia
            log.info("CASO 6 – fallback con distancia")
            if geo_props:
                return build_fallback_address(geo_props)
            return "Ubicación no disponible"

    # Hay calle cercana
    if lugar_en:
        # CASO 3: dirección + en lugar
        log.info("CASO 3 – dirección + en lugar")
        return f"{calle_base}, {lugar_en['name']}"

    if esquina_en:
        # CASO 1a: en la esquina
        log.info("CASO 1a – esquina")
        return f"Esquina {calle_principal} y {esquina_en['name']}"

    if esquina_prox:
        # CASO 2: dirección + próximo a calle (prioridad sobre lugar próximo)
        log.info("CASO 2 – próximo a calle")
        return f"{calle_base}, próximo a {esquina_prox['name']}"

    if lugar_prox:
        # CASO 4: dirección + próximo a lugar
        log.info("CASO 4 – próximo a lugar")
        return f"{calle_base}, próximo a {lugar_prox['name']}"

    # CASO 1b: solo dirección
    log.info("CASO 1b – solo dirección")
    return calle_base


# ── TTS ───────────────────────────────────────────────────────────────────────

def text_to_wav(text):
    log.info("Generando audio: %s", text)

    # Intento 1: gTTS + ffmpeg
    try:
        mp3_buf = io.BytesIO()
        gTTS(text=text, lang=TTS_LANG, slow=False).write_to_fp(mp3_buf)
        mp3_buf.seek(0)

        import subprocess, shutil
        if shutil.which("ffmpeg"):
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(mp3_buf.read())
                tmp_mp3 = f.name
            tmp_wav = tmp_mp3.replace(".mp3", ".wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_mp3,
                 "-ar", "22050", "-ac", "1", "-acodec", "pcm_u8", tmp_wav],
                check=True, capture_output=True
            )
            wav = open(tmp_wav, "rb").read()
            os.unlink(tmp_mp3)
            os.unlink(tmp_wav)
            log.info("WAV via gTTS+ffmpeg (%d bytes)", len(wav))
            return wav, "audio/wav"
        else:
            return mp3_buf.getvalue(), "audio/mpeg"

    except Exception as e:
        log.warning("gTTS falló (%s), usando pyttsx3", e)

    # Intento 2: pyttsx3 offline
    import pyttsx3
    engine = pyttsx3.init()
    engine.setProperty("rate", 150)
    for v in engine.getProperty("voices"):
        if "spanish" in v.name.lower():
            engine.setProperty("voice", v.id)
            break
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_wav = f.name
    engine.save_to_file(text, tmp_wav)
    engine.runAndWait()
    engine.stop()
    wav = open(tmp_wav, "rb").read()
    os.unlink(tmp_wav)
    log.info("WAV via pyttsx3 (%d bytes)", len(wav))
    return wav, "audio/wav"


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _parse_coords(data):
    """Parsea y valida lat/lon del body JSON. Lanza ValueError si hay error."""
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("lat y lon requeridos y numéricos")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Coordenadas fuera de rango")
    return lat, lon


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/location/plain", methods=["POST"])
def location_plain():
    """Devuelve el mensaje de ubicación como texto plano. Ideal para ESP32."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return "error: se esperaba JSON", 400
    try:
        lat, lon = _parse_coords(data)
    except ValueError as e:
        return f"error: {e}", 400
    try:
        msg = build_location_message(lat, lon)
        return msg, 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        log.exception("Error en /location/plain")
        return str(e), 502


@app.route("/location/text", methods=["POST"])
def location_text():
    """Devuelve el mensaje de ubicación como JSON."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Se esperaba JSON"}), 400
    try:
        lat, lon = _parse_coords(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        msg = build_location_message(lat, lon)
        return jsonify({"lat": lat, "lon": lon, "address": msg})
    except Exception as e:
        log.exception("Error en /location/text")
        return jsonify({"error": str(e)}), 502


@app.route("/location", methods=["POST"])
def location():
    """Devuelve un archivo WAV con la ubicación hablada."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Se esperaba JSON con 'lat' y 'lon'"}), 400
    try:
        lat, lon = _parse_coords(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    log.info("Solicitud: lat=%.6f lon=%.6f", lat, lon)

    try:
        msg = build_location_message(lat, lon)
    except Exception as e:
        log.exception("Error construyendo mensaje")
        return jsonify({"error": str(e)}), 502

    try:
        audio_bytes, mime_type = text_to_wav(msg)
    except Exception as e:
        log.exception("Error generando audio")
        return jsonify({"error": str(e)}), 500

    ext = "wav" if "wav" in mime_type else "mp3"
    return send_file(
        io.BytesIO(audio_bytes),
        mimetype=mime_type,
        as_attachment=True,
        download_name=f"location.{ext}"
    )


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    if not GEOAPIFY_API_KEY:
        log.warning("GEOAPIFY_API_KEY no configurada")
    log.info("Iniciando en puerto %d", port)
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "false") == "true")
