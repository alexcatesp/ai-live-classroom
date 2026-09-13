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
reinícialo.

**El modelo del aula.** La app usa `qwen3.8-aula`, una variante de
`qwen3.8:27b` creada con [`ollama/Modelfile.aula`](ollama/Modelfile.aula):
comparte los pesos, no ocupa más disco y fija un contexto de 8.192 tokens y un
tope de 350 tokens por respuesta. El `qwen3.8:27b` original queda intacto para
otras aplicaciones que necesitan más contexto, como Hermes Desktop.

```bash
ollama create qwen3.8-aula -f docs/ollama/Modelfile.aula
```

Las dos variantes no caben a la vez en 16 GB: si otra aplicación tiene cargado
el original, Ollama lo descarga para cargar el del aula, y al revés. La app
precarga el modelo al iniciar la clase.

**Modelo de transcripción.** La aplicación pide
`deepdml/faster-whisper-large-v3-turbo-ct2` por defecto. Descárgalo en speaches
con su herramienta de modelos (`speaches-cli model download <id>`, ver su
documentación) o cambia el nombre en Configuración por otro que ya tengas.

**Medido en la RTX 5070 Ti (13/09/2026), `qwen3.8:27b` Q2_K:**

| | Contexto 65.536 (original) | Contexto 8.192 (`qwen3.8-aula`) |
|---|---|---|
| VRAM del modelo | 13,7 GB | 9,3 GB |
| Primera palabra, con el modelo cargado | — | 0,35–0,8 s |
| Primera frase completa | — | ≈ 1,4 s |
| Velocidad | — | 52–57 tokens/s |
| Carga desde disco | 72 s la primera vez; 10 s con el archivo en caché | |

Con 9,3 GB quedan unos 4,8 GB para Whisper large-v3-turbo (~2 GB) y Kokoro
(~1 GB). La calidad en español es buena para clase, con alguna imprecisión
propia de una cuantización a 2 bits.

## 3. La aplicación

En **Configuración → Dónde se responden las preguntas**, elige **En mi
servidor** y rellena:

| Campo | Ejemplo |
|---|---|
| Transcripción (speaches) | `http://pc-casa:8000` |
| Modelo de transcripción | `deepdml/faster-whisper-large-v3-turbo-ct2` |
| Modelo de lenguaje (Ollama) | `http://pc-casa:11434` |
| Modelo de Ollama | `qwen3.8-aula` |
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
