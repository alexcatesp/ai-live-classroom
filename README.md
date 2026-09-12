# AI Classroom Live

Asistente oral para el aula: permanece en silencio durante la clase y solo
interviene cuando alguien dice **«Oye Chat»**. Aplicación de escritorio portable
para Windows, pensada para ejecutarse en los equipos de un instituto sin
permisos de administrador.

> **Estado: Fase 0 (prueba técnica).** Esta versión comprueba que la aplicación
> funciona en los equipos del centro: diagnóstico, palabra de activación y
> carpeta portable. **Todavía no responde a preguntas**; la conversación es
> trabajo de la Fase 1. Ver [`docs/decisiones-tecnicas.md`](docs/decisiones-tecnicas.md).

## Qué incluye la Fase 0

| Requisito (D-02) | Dónde está |
|---|---|
| Diagnóstico completo: micrófono, altavoces, DNS, certificados, API y clave | `backend/src/aiclassroom/diagnostics/` |
| Palabra de activación «Oye Chat» detectada en local | `backend/src/aiclassroom/audio/wakeword.py` |
| Carpeta portable verificada en un equipo sin Python ni administrador | `.github/workflows/ci.yml`, `scripts/build-portable.ps1` |
| Máquina de estados visible en todo momento (spec §7) | `backend/src/aiclassroom/session/state.py` |

La conexión con la API Realtime se verifica, pero no se conversa: el diagnóstico
abre la sesión, confirma la clave y el modelo, y la cierra (D-03).

## Arquitectura

```
┌─────────────────────────────────────────────┐
│ AI-Classroom-Live.exe        (Tauri 2, Rust)│
│  · arranca el backend como proceso hijo     │
│  · le pasa la carpeta data\                 │
│  · recibe puerto + token por stdout         │
│  · abre la ventana con esos datos inyectados│
└───────────────────┬─────────────────────────┘
                    │ HTTP + WebSocket en 127.0.0.1
┌───────────────────┴─────────────────────────┐
│ runtime\backend\  (Python empaquetado)      │
│  · motor de audio      PortAudio            │
│  · palabra clave       openWakeWord         │
│  · cliente Realtime    WebSocket            │
│  · diagnóstico, configuración, estados      │
└─────────────────────────────────────────────┘
```

Todo el audio vive en Python (D-05) y la clave de la API nunca sale del backend
(spec §4.2): el frontend solo sabe si hay una clave guardada, nunca cuál es.
Cada pieza —motor de audio, detector, cliente de IA— está detrás de una interfaz
con su doble de prueba, que es lo que permite ejecutar toda la suite en Linux
sin tarjeta de sonido.

## Desarrollo

Requisitos en la máquina de desarrollo (no en el equipo del aula): Python 3.11+,
Node.js 20+, Rust estable y, en Linux, `libwebkit2gtk-4.1-dev libgtk-3-dev`.

```bash
# Backend
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # 153 tests, 1 omitido
.venv/bin/python -m ruff check src tests

# Frontend
cd frontend
npm ci
npm run test                            # 56 tests
npm run build                           # incluye la comprobación de tipos

# Shell de escritorio
cd src-tauri
cargo clippy --all-targets
```

Para levantar la aplicación entera en desarrollo hacen falta las dos mitades:

```bash
# terminal 1: el backend anuncia su puerto y su token por stdout
cd backend && .venv/bin/python -m aiclassroom.main --verbose

# terminal 2: el frontend, con esos dos valores
cd frontend
VITE_BACKEND_PORT=<puerto> VITE_BACKEND_TOKEN=<token> npm run dev
```

## Construir la versión portable

La construcción debe hacerse en Windows (D-09). Hay dos caminos:

**En tu propio equipo Windows:**

```powershell
.\scripts\build-portable.ps1
```

Deja el resultado en `release\AI-Classroom-Live\` y un zip junto a él.

**En CI:** cada push construye la carpeta y la publica como artefacto del
workflow *CI*, trabajo «Carpeta portable para Windows». Descárgalo, descomprime
y ejecuta `AI-Classroom-Live.exe` en un PC del instituto.

El trabajo de Windows no se limita a compilar: ejecuta el backend empaquetado
con `--selftest --require-audio`, de modo que un build sin PortAudio —una
aplicación incapaz de abrir el micrófono— falla en el CI y no en el aula.

### Los modelos de la palabra de activación

Ninguno está en el repositorio. Hacen falta dos cosas en `data\models\`:

**1. El extractor de características de openWakeWord** (común a cualquier
frase). Lo descarga la construcción portable automáticamente, pero puedes
hacerlo a mano:

```bash
python scripts/fetch_wakeword_runtime.py --output data/models
```

Se descargan en la máquina de construcción a propósito: así el equipo del aula
no depende de la red para arrancar el detector.

**2. El modelo de la frase «Oye Chat»**, que se entrena una vez:

```bash
python -m venv .venv-train
.venv-train/bin/pip install "openwakeword[training]"
python scripts/train_wakeword.py --phrase "Oye Chat" --output data/models
```

El entrenamiento arrastra torch y un modelo de síntesis de voz, así que vive
fuera de la aplicación y fuera del CI.

## Estructura

```
backend/     motor local en Python: audio, detector, diagnóstico, estados
frontend/    interfaz React + TypeScript
src-tauri/   contenedor de escritorio en Rust
scripts/     construcción portable y entrenamiento del detector
docs/        especificación y registro de decisiones técnicas
```

## Privacidad

Durante la escucha pasiva no sale audio del equipo: la palabra de activación se
detecta en local y los fragmentos se descartan según se procesan. No se guarda
audio, y las transcripciones solo se conservan si el profesor lo activa
(spec §14). La clave de la API se guarda cifrada con la cuenta de Windows del
equipo, sin necesidad de permisos de administrador (D-07).

## Riesgos abiertos

Los recoge [`docs/decisiones-tecnicas.md`](docs/decisiones-tecnicas.md). El
principal sigue siendo **R-1**: la tasa de falsos positivos de «Oye Chat» en un
aula con ruido, que solo puede medirse en un aula real. El medidor del detector
está en la aplicación precisamente para eso.
