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

# Voz (Kokoro). En una RTX 50xx (Blackwell), la imagen -cu128.
docker run --detach --restart unless-stopped --name kokoro \
  --publish 8880:8880 --gpus all \
  ghcr.io/remsky/kokoro-fastapi-gpu:v0.9.0-cu128
```

**RTX 50xx (Blackwell).** La imagen `kokoro-fastapi-gpu:latest` se reinicia en
bucle con `CUDA error: no kernel image is available for execution on the
device`: su PyTorch no está compilado para Blackwell. Hay que usar la variante
`-cu128`. speaches `latest-cuda` sí funciona.

**Antivirus que inspecciona HTTPS (Avast).** speaches descarga los modelos de
huggingface.co, y dentro del contenedor falla con `CERTIFICATE_VERIFY_FAILED`:
Avast firma el tráfico HTTPS con su propia raíz, en la que Windows confía y el
contenedor no (R-3, R-8). Sin tocar el antivirus, se exporta esa raíz, se une a
los certificados habituales y se monta en el contenedor:

```powershell
$dir = "$env:USERPROFILE\.docker-certs"; New-Item -ItemType Directory -Force $dir
$avast = Get-ChildItem Cert:\LocalMachine\Root | Where-Object Subject -like "*Avast Web/Mail Shield Root*"
$pem = "-----BEGIN CERTIFICATE-----`n" + [Convert]::ToBase64String($avast.RawData, 'InsertLineBreaks') + "`n-----END CERTIFICATE-----`n"
# certifi.where() de cualquier Python con certifi da el paquete habitual
$bundle = (Get-Content (python -c "import certifi; print(certifi.where())") -Raw) + "`n" + $pem
[IO.File]::WriteAllText("$dir\ca-bundle.pem", ($bundle -replace "`r`n", "`n"))
```

```bash
docker run --detach --restart unless-stopped --name speaches \
  --publish 8000:8000 --gpus=all \
  --volume hf-hub-cache:/home/ubuntu/.cache/huggingface/hub \
  --volume "C:/Users/<usuario>/.docker-certs/ca-bundle.pem:/etc/ssl/certs/ca-bundle-local.pem:ro" \
  -e REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-bundle-local.pem \
  -e SSL_CERT_FILE=/etc/ssl/certs/ca-bundle-local.pem \
  ghcr.io/speaches-ai/speaches:latest-cuda

# Descargar el modelo de transcripción (unos 2 minutos)
curl -X POST http://localhost:8000/v1/models/deepdml/faster-whisper-large-v3-turbo-ct2
```

Ollama se instala directamente en Windows desde ollama.com. Para que escuche
fuera de `localhost`, define la variable de entorno `OLLAMA_HOST=0.0.0.0` y
reinícialo.

**El modelo del aula: `gemma4-aula`.** Medido en la RTX 5070 Ti el 15/09/2026,
con contexto de 8.192 y sin razonar, gemma4 gana a Qwen en todo lo que importa
aquí:

| | `qwen3.8-aula` | `gemma4-aula` |
|---|---|---|
| VRAM | 9,3 GB | **7,8 GB** |
| Velocidad | 55 tok/s | **76 tok/s** |
| Primera frase | ~1,4 s | **0,5–0,9 s** |
| Respuesta | correcta | correcta, 74–90 palabras |

Esos 1,5 GB de menos son los que permiten que Whisper y Kokoro compartan la
tarjeta. Necesita **Ollama 0.34 o posterior**: la 0.33 no cargaba el componente
de visión de gemma4 («Failed to load CLIP model»).

```bash
ollama create gemma4-aula -f docs/ollama/Modelfile.aula-gemma
```

Sin el `num_ctx` del Modelfile, Ollama le da 262.144 tokens de contexto y la
caché se lleva 4,5 GB más.

**La alternativa, `qwen3.8-aula`**, es una variante de
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
`deepdml/faster-whisper-large-v3-turbo-ct2` por defecto; se descarga con el
`POST /v1/models/<id>` de arriba.

**Medido en la RTX 5070 Ti (13/09/2026), `qwen3.8:27b` Q2_K:**

| | Contexto 65.536 (original) | Contexto 8.192 (`qwen3.8-aula`) |
|---|---|---|
| VRAM del modelo | 13,7 GB | 9,3 GB |
| Primera palabra, con el modelo cargado | — | 0,35–0,8 s |
| Primera frase completa | — | ≈ 1,4 s |
| Velocidad | — | 52–57 tokens/s |
| Carga desde disco | 72 s la primera vez; 10 s con el archivo en caché | |

**Los tres servicios juntos, medido de principio a fin (14/09/2026).** La pregunta
la dijo Kokoro con otra voz y entró en la sesión local de la app como si viniera
del micrófono, con Silero decidiendo el final:

| | 1.ª pregunta | 2.ª pregunta (repregunta) |
|---|---|---|
| Transcripción | 3,9 s (Whisper cargándose) | 0,6 s |
| Primer audio tras acabar la pregunta | 5,6 s | **1,8 s** |
| Respuesta completa generada | 6,6 s | 2,9 s |
| Voz de la respuesta | 28 s | 26 s |
| VRAM total de la tarjeta | **15,1 GB de 16,3** | |

- Los dos segundos de silencio que cierran la pregunta van antes de esas cifras.
- La repregunta («¿y puedes ponerme un ejemplo de eso?») se entendió gracias al
  historial.
- Con 15,1 GB ocupados no queda sitio para subir el contexto de Qwen a 16.384
  tokens (≈ 0,6 GB más) sin arriesgarse a que Windows lo pase a la RAM.
- La primera pregunta paga la carga de Whisper; calentar los tres servicios al
  arrancar la app lo evita (pendiente).

**El techo de memoria dentro de Docker (15/09/2026).** Con los tres modelos en
la GPU la tarjeta llega a 12,9–13,0 GB de 16,3, y aun así Whisper falla al
transcribir con `CUDA failed with error out of memory`: la máquina virtual de
WSL no puede usar los últimos ~3 GB que Windows reserva. Dos veces, además, el
error se llevó por delante **todo el motor de Docker**, Kokoro incluido.

Lo medido al buscar margen:

- **Qwen con capas en la CPU no sirve:** 7 capas de 65 liberan 0,9 GB y bajan de
  56 a 6 tokens/s. La voz se pararía entre frases.
- **Whisper en la CPU funciona pero es lento:** 12–14 s por pregunta con
  `large-v3-turbo`, 10 s con `medium`.
- **Kokoro en la CPU:** 2,5 s una frase corta y 5,5 s una larga (2x tiempo
  real), frente a 0,15 s en la GPU.
- **El escritorio pesa:** con navegadores, Hermes y otras aplicaciones abiertas
  ocupa 2,5–2,7 GB de la tarjeta; con el PC despejado, 1,5 GB.

Mientras no haya más margen, la combinación estable es dejar en la CPU el
servicio que menos duela, y el PC del servidor lo más despejado posible durante
las clases.
- La calidad en español es buena para clase, con alguna errata propia de 2 bits
  («explícamente»).

## 3. La aplicación

En **Configuración → Dónde se responden las preguntas**, elige **En mi
servidor** y rellena:

| Campo | Ejemplo |
|---|---|
| Transcripción (speaches) | `http://pc-casa:8000` |
| Modelo de transcripción | `deepdml/faster-whisper-large-v3-turbo-ct2` |
| Modelo de lenguaje (Ollama) | `http://pc-casa:11434` |
| Modelo de Ollama | `gemma4-aula` |
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
