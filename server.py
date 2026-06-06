"""
GPS Audio Server
Recibe coordenadas GPS → Geoapify (reverse geocoding) → TTS → WAV
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

GEOAPIFY_API_KEY = os.environ.get("GEOAPIFY_API_KEY", "")
TTS_LANG         = "es"


# ── Geocoding ─────────────────────────────────────────────────────────────────

def reverse_geocode(lat, lon):
    url = "https://api.geoapify.com/v1/geocode/reverse"
    params = {
        "lat": lat,
        "lon": lon,
        "apiKey": GEOAPIFY_API_KEY,
        "lang": "es",
        "type": "amenity",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    features = data.get("features", [])
    if not features:
        raise ValueError("Geoapify: sin resultados")

    props = features[0]["properties"]
    street      = props.get("street")
    housenumber = props.get("housenumber")
    city        = props.get("city") or props.get("town") or props.get("village")

    parts = []
    if street:
        parts.append(f"{street} {housenumber}" if housenumber else street)
    if city:
        parts.append(city)

    result = ", ".join(parts) if parts else props.get("formatted", "Dirección desconocida")
    log.info("Dirección: %s", result)
    return result


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

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/location/plain", methods=["POST"])
def location_plain():
    """Devuelve la dirección como texto plano. Ideal para ESP32."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return "error: se esperaba JSON", 400
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return "error: lat y lon requeridos", 400
    try:
        address = reverse_geocode(lat, lon)
        return address, 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        return str(e), 502


@app.route("/location/text", methods=["POST"])
def location_text():
    """Devuelve la dirección como JSON."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Se esperaba JSON"}), 400
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat y lon requeridos"}), 400
    try:
        address = reverse_geocode(lat, lon)
        return jsonify({"lat": lat, "lon": lon, "address": address})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/location", methods=["POST"])
def location():
    """Devuelve un archivo WAV con la dirección hablada."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Se esperaba JSON con 'lat' y 'lon'"}), 400
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat y lon requeridos y numéricos"}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "Coordenadas fuera de rango"}), 400

    log.info("Solicitud: lat=%.6f lon=%.6f", lat, lon)

    try:
        address = reverse_geocode(lat, lon)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    try:
        audio_bytes, mime_type = text_to_wav(f"Estás en {address}, mi nigga")
    except Exception as e:
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
