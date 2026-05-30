"""
GPS Audio Server
Recibe coordenadas GPS, obtiene la dirección via reverse geocoding,
genera un audio WAV con TTS y lo devuelve al cliente.

Orden de geocoding:
  1. Google Maps  (si GOOGLE_API_KEY está configurada)
  2. Geoapify     (si GEOAPIFY_API_KEY está configurada)
  3. Photon       (gratis, sin key, basado en OSM)
  4. Nominatim    (gratis, sin key, fallback final)
"""

import io
import os
import logging
import tempfile
from flask import Flask, request, send_file, jsonify
import requests
from gtts import gTTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

GOOGLE_API_KEY   = os.environ.get("GOOGLE_API_KEY", "")
GEOAPIFY_API_KEY = os.environ.get("GEOAPIFY_API_KEY", "d6caba5d7b274f5ea6e191467d43c363")
TTS_LANG         = "es"


# ── Geocoding engines ─────────────────────────────────────────────────────────

def reverse_geocode_google(lat, lon):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"latlng": f"{lat},{lon}", "key": GOOGLE_API_KEY,
              "language": "es", "result_type": "street_address|route"}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK" or not data.get("results"):
        raise ValueError(f"Google status: {data.get('status')}")
    c = data["results"][0]["address_components"]
    number   = next((x["long_name"] for x in c if "street_number" in x["types"]), None)
    route    = next((x["long_name"] for x in c if "route"          in x["types"]), None)
    locality = next((x["long_name"] for x in c if "locality"       in x["types"]), None)
    parts = []
    if route:
        parts.append(f"{route} {number}" if number else route)
    if locality:
        parts.append(locality)
    result = ", ".join(parts) if parts else data["results"][0]["formatted_address"]
    log.info("[Google]    %s", result)
    return result


def reverse_geocode_geoapify(lat, lon):
    url = "https://api.geoapify.com/v1/geocode/reverse"
    params = {"lat": lat, "lon": lon, "apiKey": GEOAPIFY_API_KEY,
              "lang": "es", "type": "amenity"}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features", [])
    if not features:
        raise ValueError("Geoapify: sin resultados")
    props = features[0]["properties"]
    street  = props.get("street")
    number  = props.get("housenumber") or props.get("address_line1", "").split()[-1]
    city    = props.get("city") or props.get("town") or props.get("village")
    parts = []
    if street:
        # Intentar obtener el número más cercano si no hay uno exacto
        housenumber = props.get("housenumber")
        parts.append(f"{street} {housenumber}" if housenumber else street)
    if city:
        parts.append(city)
    result = ", ".join(parts) if parts else props.get("formatted", "Dirección desconocida")
    log.info("[Geoapify]  %s", result)
    return result


def reverse_geocode_photon(lat, lon):
    url = "https://photon.komoot.io/reverse"
    params = {"lat": lat, "lon": lon, "lang": "es"}
    headers = {"User-Agent": "GPS-Audio-Server/1.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features", [])
    if not features:
        raise ValueError("Photon: sin resultados")
    p = features[0]["properties"]
    street = p.get("street")
    number = p.get("housenumber")
    city   = p.get("city") or p.get("town") or p.get("village")
    parts = []
    if street:
        parts.append(f"{street} {number}" if number else street)
    if city:
        parts.append(city)
    result = ", ".join(parts) if parts else "Dirección desconocida"
    log.info("[Photon]    %s", result)
    return result


def reverse_geocode_nominatim(lat, lon):
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": lat, "lon": lon, "format": "json", "addressdetails": 1, "zoom": 18}
    headers = {"User-Agent": "GPS-Audio-Server/1.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    a = data.get("address", {})
    road   = a.get("road") or a.get("pedestrian") or a.get("path")
    number = a.get("house_number")
    city   = a.get("city") or a.get("town") or a.get("village")
    parts = []
    if road:
        parts.append(f"{road} {number}" if number else road)
    if city:
        parts.append(city)
    result = ", ".join(parts) if parts else data.get("display_name", "Dirección desconocida")
    log.info("[Nominatim] %s", result)
    return result


def reverse_geocode(lat, lon):
    """Prueba cada engine en orden, devuelve el primero que funcione."""
    engines = []
    if GOOGLE_API_KEY:
        engines.append(("Google",    reverse_geocode_google))
    if GEOAPIFY_API_KEY:
        engines.append(("Geoapify",  reverse_geocode_geoapify))
    engines.append(("Photon",    reverse_geocode_photon))
    engines.append(("Nominatim", reverse_geocode_nominatim))

    last_error = None
    for name, fn in engines:
        try:
            return fn(lat, lon)
        except Exception as e:
            log.warning("%s falló: %s", name, e)
            last_error = e

    raise RuntimeError(f"Todos los engines fallaron. Último error: {last_error}")


# ── TTS ───────────────────────────────────────────────────────────────────────

def text_to_wav(text):
    log.info("Generando audio para: %s", text)
    try:
        mp3_buf = io.BytesIO()
        gTTS(text=text, lang=TTS_LANG, slow=False).write_to_fp(mp3_buf)
        mp3_buf.seek(0)
        import subprocess, shutil
        if shutil.which("ffmpeg"):
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(mp3_buf.read()); tmp_mp3 = f.name
            tmp_wav = tmp_mp3.replace(".mp3", ".wav")
            subprocess.run(["ffmpeg", "-y", "-i", tmp_mp3, "-ar", "8000", "-ac", "1", "-acodec", "pcm_u8", tmp_wav],
                           check=True, capture_output=True)
            wav = open(tmp_wav, "rb").read()
            os.unlink(tmp_mp3); os.unlink(tmp_wav)
            log.info("WAV via gTTS+ffmpeg (%d bytes)", len(wav))
            return wav, "audio/wav"
        else:
            return mp3_buf.getvalue(), "audio/mpeg"
    except Exception as e:
        log.warning("gTTS falló (%s), usando pyttsx3", e)

    import pyttsx3
    engine = pyttsx3.init()
    engine.setProperty("rate", 150)
    for v in engine.getProperty("voices"):
        if "spanish" in v.name.lower():
            engine.setProperty("voice", v.id); break
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_wav = f.name
    engine.save_to_file(text, tmp_wav)
    engine.runAndWait(); engine.stop()
    wav = open(tmp_wav, "rb").read()
    os.unlink(tmp_wav)
    log.info("WAV via pyttsx3 (%d bytes)", len(wav))
    return wav, "audio/wav"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "geocoding_engines": {
            "google":   "activo" if GOOGLE_API_KEY   else "sin key",
            "geoapify": "activo" if GEOAPIFY_API_KEY else "sin key",
            "photon":   "activo (sin key)",
            "nominatim":"activo (sin key)",
        }
    })


@app.route("/location", methods=["POST"])
def location():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Se esperaba JSON con 'lat' y 'lon'"}), 400
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Campos 'lat' y 'lon' requeridos y numéricos"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "Coordenadas fuera de rango"}), 400

    log.info("Solicitud: lat=%.6f lon=%.6f", lat, lon)
    try:
        address = reverse_geocode(lat, lon)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    try:
        audio_bytes, mime_type = text_to_wav(f"Ubicación: {address}")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    ext = "wav" if "wav" in mime_type else "mp3"
    return send_file(io.BytesIO(audio_bytes), mimetype=mime_type,
                     as_attachment=True, download_name=f"location.{ext}")


@app.route("/location/text", methods=["POST"])
def location_text():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Se esperaba JSON"}), 400
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Campos 'lat' y 'lon' requeridos"}), 400
    try:
        address = reverse_geocode(lat, lon)
        return jsonify({"lat": lat, "lon": lon, "address": address})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# Endpoint extra: comparar todos los engines a la vez
@app.route("/location/compare", methods=["POST"])
def location_compare():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Se esperaba JSON"}), 400
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Campos 'lat' y 'lon' requeridos"}), 400

    results = {}
    engines = []
    if GOOGLE_API_KEY:
        engines.append(("google",    reverse_geocode_google))
    if GEOAPIFY_API_KEY:
        engines.append(("geoapify",  reverse_geocode_geoapify))
    engines.append(("photon",    reverse_geocode_photon))
    engines.append(("nominatim", reverse_geocode_nominatim))

    for name, fn in engines:
        try:
            results[name] = fn(lat, lon)
        except Exception as e:
            results[name] = f"ERROR: {e}"

    return jsonify({"lat": lat, "lon": lon, "results": results})


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    if not GOOGLE_API_KEY:
        log.warning("GOOGLE_API_KEY no configurada")
    if not GEOAPIFY_API_KEY:
        log.warning("GEOAPIFY_API_KEY no configurada — registrate en myprojects.geoapify.com (gratis)")
    log.info("Iniciando GPS Audio Server en puerto %d", port)
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG","false")=="true")
