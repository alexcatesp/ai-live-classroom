# Servidor local: faster-whisper, Qwen y Kokoro

Guía para montar en el PC de casa (RTX 5070 Ti) la alternativa sin coste por uso
a la API de OpenAI (D-14). La aplicación del aula habla con tres servicios a
través de Tailscale:

| Servicio | Qué hace | Proyecto | Puerto |
|---|---|---|---|
| Transcripción | Audio de la pregunta → texto | [speaches](https://speaches.ai/) (faster-whisper) | 8000 |
| Modelo de lenguaje | Texto → respuesta | [Ollama](https://ollama.com/) con Qwen | 11434 |
| Voz | Respuesta → audio PCM a 24 kHz | [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) | 8880 |

Cada servicio tiene su propia dirección en Configuración, así que pueden estar en
puertos o equipos distintos.

## 1. Tailscale

1. Instala Tailscale en el PC de casa y en el equipo del aula, con la misma
   cuenta.
2. Anota el nombre del PC de casa en la red de Tailscale (por ejemplo
   `pc-casa`) o su IP `100.x.y.z`.
3. Desde el equipo del aula, comprueba que responde: `ping pc-casa`.

El tráfico va cifrado por WireGuard, así que las direcciones pueden ser `http://`.

**Riesgo del instituto:** si la red bloquea el UDP, Tailscale pasa por sus
relés DERP sobre el puerto 443. Funciona, pero con más latencia. Hay que
comprobarlo en el aula antes de depender de ello.

## 2. Los servicios

Con Docker Desktop y soporte de GPU (WSL2 en Windows):

```bash
# Transcripción (faster-whisper). Los modelos se guardan en el volumen.
docker run --detach --restart unless-stopped --name speaches \
  --publish 8000:8000 --gpus=all \
  --volume hf-hub-cache:/home/ubuntu/.cache/huggingface/hub \
  ghcr.io/speaches-ai/speaches:latest-cuda

# Voz (Kokoro).
docker run --detach --restart unless-stopped --name kokoro \
  --publish 8880:8880 --gpus all \
  ghcr.io/remsky/kokoro-fastapi-gpu:latest
```

Ollama se instala directamente en Windows desde ollama.com. Para que escuche
fuera de `localhost`, define la variable de entorno `OLLAMA_HOST=0.0.0.0` y
reinícialo. Después:

```bash
ollama pull qwen3:14b
```

**Modelo de transcripción.** La aplicación pide
`deepdml/faster-whisper-large-v3-turbo-ct2` por defecto. Descárgalo en speaches
con su herramienta de modelos (`speaches-cli model download <id>`, ver su
documentación) o cambia el nombre en Configuración por otro que ya tengas.

**Memoria de vídeo (16 GB).** Whisper large-v3-turbo ocupa unos 2 GB, Kokoro
alrededor de 1 GB y `qwen3:14b` cuantizado unos 9–10 GB: caben a la vez. Si
falta memoria o la respuesta tarda en empezar, `qwen3:8b` es la alternativa.

## 3. La aplicación

En **Configuración → Dónde se responden las preguntas**, elige **En mi
servidor** y rellena:

| Campo | Ejemplo |
|---|---|
| Transcripción (speaches) | `http://pc-casa:8000` |
| Modelo de transcripción | `deepdml/faster-whisper-large-v3-turbo-ct2` |
| Modelo de lenguaje (Ollama) | `http://pc-casa:11434` |
| Modelo de Ollama | `qwen3:14b` |
| Voz (Kokoro) | `http://pc-casa:8880` |
| Voz de Kokoro | `ef_dora` (también `em_alex`, `em_santa`) |

La elección se guarda en `data/config/settings.json` y se mantiene entre
sesiones.

Pulsa **Comprobar equipo**. En modo local, el diagnóstico no comprueba la clave
ni la conexión con OpenAI, sino que el servidor responde y que Ollama tiene el
modelo.

## Qué cambia respecto a la nube

- **Coste:** ninguno por uso; solo la luz del PC de casa.
- **Privacidad:** igual que en la nube, solo sale audio desde «Oye Chat» hasta
  el final de la pregunta, pero va a tu PC, no a OpenAI.
- **Fin de la pregunta:** lo decide el equipo del aula con Silero, el mismo
  filtro de voz del detector, tras el silencio configurado (`turn_silence_ms`).
- **Latencia:** la respuesta no puede empezar hasta que la pregunta termina y se
  transcribe. La voz empieza en cuanto Qwen escribe la primera frase, sin
  esperar al resto.
- **Interrupción:** «Oye Chat» y **Parar** funcionan igual. En el historial
  solo queda lo que llegó a oírse de la respuesta cortada.
- **«Probar conversación»** sigue usando OpenAI.
- **Sin servidor:** si el PC no responde, la clase empieza igualmente, escucha y
  avisa en cada activación mientras reintenta cada 5 s, como sin red en la nube.
