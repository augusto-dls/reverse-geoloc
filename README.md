# GPS Audio Server

Recibe coordenadas GPS → obtiene dirección (reverse geocoding) → genera audio WAV/MP3 con TTS → lo devuelve al cliente.

## Estructura

```
gps-audio-server/
├── server.py          # Servidor Flask principal
├── test_client.py     # Cliente de prueba desde terminal
├── requirements.txt
├── render.yaml        # Config para deploy en Render.com
└── README.md
```

## Fase 1 — Prueba local

### 1. Instalar dependencias

```bash
pip install -r requirements.txt

# ffmpeg es opcional pero convierte MP3 → WAV nativo (mejor para ESP32)
# Linux:
sudo apt install ffmpeg
# macOS:
brew install ffmpeg
# Windows: https://ffmpeg.org/download.html
```

### 2. Levantar el servidor

```bash
python server.py
# → Servidor en http://localhost:5000
```

### 3. Probar desde la terminal

```bash
# Córdoba, Argentina (por defecto)
python test_client.py

# Buenos Aires
python test_client.py --lat -34.6037 --lon -58.3816

# Solo texto (sin audio)
python test_client.py --text-only

# Sin reproducir (solo guardar el archivo)
python test_client.py --no-play

# Guardar con nombre específico
python test_client.py --output mi_ubicacion
```

### 4. También con curl

```bash
# Obtener dirección en texto
curl -X POST http://localhost:5000/location/text \
  -H "Content-Type: application/json" \
  -d '{"lat": -31.4201, "lon": -64.1888}'

# Obtener audio
curl -X POST http://localhost:5000/location \
  -H "Content-Type: application/json" \
  -d '{"lat": -31.4201, "lon": -64.1888}' \
  --output audio.wav
```

## Fase 2 — Deploy en Render.com

1. Crear repo en GitHub con estos archivos
2. En [render.com](https://render.com) → New → Web Service → conectar repo
3. Render detecta `render.yaml` automáticamente
4. El servicio queda en `https://tu-app.onrender.com`

Probar con:
```bash
python test_client.py --host https://tu-app.onrender.com --lat -31.42 --lon -64.19
```

## Fase 3 — ESP32

El ESP32 debe:
1. Detectar botón (GPIO con pull-up)
2. Leer NMEA del NEO-6M (UART2: TX=17, RX=16)
3. Parsear `$GPRMC` o `$GPGGA` para extraer lat/lon
4. Conectar al hotspot y hacer POST a `/location`
5. Recibir bytes del WAV/MP3
6. Reproducir:
   - **DAC interno** (GPIO25/26): WAV 8-bit, 8kHz, mono → simple pero calidad baja
   - **I2S + MAX98357A**: WAV 16-bit, 22kHz → calidad buena, recomendado
   - **PWM → filtro RC → amplificador**: alternativa económica

## Endpoints

| Método | Ruta | Body | Respuesta |
|--------|------|------|-----------|
| GET | `/health` | — | `{"status":"ok"}` |
| POST | `/location` | `{"lat": float, "lon": float}` | archivo WAV o MP3 |
| POST | `/location/text` | `{"lat": float, "lon": float}` | `{"address": "..."}` |

## Notas

- **Geocoding**: usa Nominatim (OpenStreetMap), gratuito, sin API key. Límite: 1 req/seg.
- **TTS**: gTTS (Google TTS), requiere internet en el servidor. Idioma configurable (`TTS_LANG` en server.py).
- **Formato audio**: WAV si ffmpeg está instalado, MP3 de lo contrario.
- **Plan free de Render**: el servidor duerme tras 15 min de inactividad. Primera llamada tarda ~30s en despertar. Para el ESP32 conviene el plan Starter.
