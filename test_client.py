#!/usr/bin/env python3
"""
test_client.py  —  Prueba del GPS Audio Server desde la terminal.

Uso:
    python test_client.py                          # coordenadas por defecto (Córdoba, AR)
    python test_client.py -lat -34.6037 -lon -58.3816   # Buenos Aires
    python test_client.py --host https://mi-app.onrender.com --lat -31.42 --lon -64.19
    python test_client.py --text-only              # solo muestra la dirección, sin descargar audio
"""

import argparse
import sys
import os
import subprocess
import platform
import requests

DEFAULT_HOST = "http://localhost:5000"
DEFAULT_LAT  = -31.423972   # Córdoba capital, AR
DEFAULT_LON  = -64.1804848


def parse_args():
    p = argparse.ArgumentParser(description="Cliente de prueba para GPS Audio Server")
    p.add_argument("--host",      default=DEFAULT_HOST,  help="URL base del servidor")
    p.add_argument("--lat", "-lat", type=float, default=DEFAULT_LAT, help="Latitud")
    p.add_argument("--lon", "-lon", type=float, default=DEFAULT_LON, help="Longitud")
    p.add_argument("--output", "-o", default="location_audio",
                   help="Nombre base del archivo de salida (sin extensión)")
    p.add_argument("--text-only", action="store_true",
                   help="Solo obtener texto de dirección, sin audio")
    p.add_argument("--no-play", action="store_true",
                   help="No reproducir el audio automáticamente")
    return p.parse_args()


def check_text(host, lat, lon):
    url = f"{host}/location/text"
    print(f"\n📡 Consultando dirección en {url}")
    print(f"   Coordenadas: lat={lat}, lon={lon}")

    resp = requests.post(url, json={"lat": lat, "lon": lon}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    print(f"\n✅ Dirección obtenida:")
    print(f"   {data['address']}")
    return data["address"]


def fetch_audio(host, lat, lon, output_base):
    url = f"{host}/location"
    print(f"\n📡 Solicitando audio en {url}")
    print(f"   Coordenadas: lat={lat}, lon={lon}")

    resp = requests.post(url, json={"lat": lat, "lon": lon}, timeout=30, stream=True)
    resp.raise_for_status()

    # Detectar extensión desde Content-Type o Content-Disposition
    content_type = resp.headers.get("Content-Type", "")
    if "wav" in content_type:
        ext = "wav"
    else:
        ext = "mp3"

    # También intentar desde Content-Disposition
    cd = resp.headers.get("Content-Disposition", "")
    if "location.wav" in cd:
        ext = "wav"
    elif "location.mp3" in cd:
        ext = "mp3"

    output_path = f"{output_base}.{ext}"
    size = 0
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=4096):
            f.write(chunk)
            size += len(chunk)

    print(f"\n✅ Audio guardado: {output_path} ({size:,} bytes, {ext.upper()})")
    return output_path, ext


def play_audio(path, ext):
    """Intenta reproducir el audio con el player disponible en el sistema."""
    system = platform.system()
    print(f"\n🔊 Reproduciendo {path}...")

    try:
        if system == "Darwin":           # macOS
            subprocess.run(["afplay", path], check=True)
        elif system == "Linux":
            if ext == "wav":
                subprocess.run(["aplay", path], check=True)
            else:
                subprocess.run(["mpg123", path], check=True)
        elif system == "Windows":
            import winsound
            if ext == "wav":
                winsound.PlaySound(path, winsound.SND_FILENAME)
            else:
                os.startfile(path)
        print("✅ Reproducción completada.")
    except FileNotFoundError:
        print(f"⚠️  Player no encontrado. Reproducí manualmente el archivo: {path}")
    except Exception as e:
        print(f"⚠️  No se pudo reproducir: {e}. Archivo guardado en: {path}")


def main():
    args = parse_args()
    host = args.host.rstrip("/")

    # 1. Verificar que el servidor esté vivo
    try:
        health = requests.get(f"{host}/health", timeout=5)
        health.raise_for_status()
        print(f"✅ Servidor activo: {host}")
    except requests.RequestException as e:
        print(f"❌ No se pudo conectar al servidor ({host}): {e}")
        print("   Asegurate de que server.py esté corriendo: python server.py")
        sys.exit(1)

    # 2. Solo texto
    if args.text_only:
        check_text(host, args.lat, args.lon)
        return

    # 3. Primero mostrar dirección como texto
    try:
        check_text(host, args.lat, args.lon)
    except Exception:
        pass  # No bloquear si falla, el audio ya incluye la dirección

    # 4. Pedir audio
    try:
        audio_path, ext = fetch_audio(host, args.lat, args.lon, args.output)
    except requests.HTTPError as e:
        print(f"❌ Error del servidor: {e.response.status_code} — {e.response.text}")
        sys.exit(1)
    except requests.RequestException as e:
        print(f"❌ Error de red: {e}")
        sys.exit(1)

    # 5. Reproducir
    if not args.no_play:
        play_audio(audio_path, ext)


if __name__ == "__main__":
    main()
