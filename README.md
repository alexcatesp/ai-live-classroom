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

La especificación completa está en
[`docs/especificacion-tecnica.md`](docs/especificacion-tecnica.md); el código la
cita por apartado («spec §14», «spec section 7»).

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

## Probarlo

### En un PC con Windows (lo que se lleva al aula)

1. Abre la pestaña **Actions** del repositorio, entra en el último run verde de
   *CI* y descarga el artefacto **`AI-Classroom-Live-portable`**.
2. Descomprime donde quieras: escritorio, una carpeta del usuario o un USB.
3. Ejecuta `AI-Classroom-Live.exe`. Windows avisará de que el programa no está
   firmado — ver [`docs/antivirus.md`](docs/antivirus.md).
4. En **Configuración**, introduce la clave de la API y elige micrófono y
   altavoces.
5. Pulsa **Comprobar equipo** y resuelve lo que salga en rojo.
6. **Preparar sesión** → **Iniciar clase**, y di «Oye Chat». El estado debe
   pasar a *Activado* y el medidor moverse.

   Si no se activa, mira el panel **Palabra de activación** de arriba abajo. La
   barra **Micrófono** debe moverse al hablar; si no se mueve, el problema es el
   dispositivo, no el detector. **Voz detectada** indica que el filtro de voz te
   oye. Si las dos cosas funcionan y el **Detector** sigue a cero, el sonido
   llega pero el modelo no reconoce la frase: sube la sensibilidad o reentrénalo
   con tu voz.

   La diagnosis del **Detector** es una puntuación, no una transcripción: en
   escucha pasiva no se transcribe nada, ni en local ni en la nube (spec §14).

El artefacto incluye el detector ya entrenado. **Reconoce voces sintéticas**,
que es con lo que se entrena en el CI: sirve para comprobar que todo funciona,
no para fiarse de él en clase. Antes de usarlo de verdad, reentrénalo con
grabaciones de personas reales (ver más abajo).

En esta fase el asistente **no responde todavía**: se activa, se queda dos
segundos en *Activado* y vuelve a la escucha pasiva, listo para la siguiente
vez. La conversación es la Fase 1, y con ella un panel con la transcripción de
cada pregunta y su respuesta (D-11).

### En Linux o macOS, sin esperar al CI

Dos terminales:

```bash
# 1. El motor local. Anuncia su puerto y su token por stdout.
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev,wakeword,train]"
.venv/bin/python ../scripts/fetch_wakeword_runtime.py --output ../data/models
.venv/bin/python ../scripts/train_wakeword.py --models ../data/models   # unos minutos
AICLASSROOM_DATA_DIR=../data .venv/bin/python -m aiclassroom.main --verbose
```

```bash
# 2. La interfaz, con el puerto y el token que imprimió el backend.
cd frontend && npm ci
VITE_BACKEND_PORT=<puerto> VITE_BACKEND_TOKEN=<token> npm run dev
```

Abre `http://localhost:1420`. Necesitas `espeak-ng` para el entrenamiento
(`apt-get install espeak-ng` o `brew install espeak-ng`).

## Validar la Fase 0

Un script comprueba de una vez todo lo que puede comprobarse sin un PC del
instituto: las suites, el ejecutable empaquetado, el detector sobre voz real y
la carpeta portable completa con sus sumas de verificación.

```bash
./scripts/validate-phase0.sh
```

Termina diciendo qué queda fuera de su alcance: abrir un micrófono de verdad,
hablar con la API, ejecutar el `.exe` en Windows y medir falsos positivos con
una clase grabada.

## Desarrollo

Requisitos en la máquina de desarrollo (no en el equipo del aula): Python 3.11+,
Node.js 20+, Rust estable y, en Linux, `libwebkit2gtk-4.1-dev libgtk-3-dev`.

```bash
# Backend
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # 258 tests; los que necesitan modelos o audio real se omiten
.venv/bin/python -m ruff check src tests

# Frontend
cd frontend
npm ci
npm run test                            # 86 tests
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

Si compilas el shell a mano, usa `cargo build --release --features custom-protocol`.
Sin esa feature la ventana busca el servidor de Vite y muestra
`ERR_CONNECTION_REFUSED` (P-11); el código se niega a compilar así.

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
sudo apt-get install espeak-ng     # las voces con las que se entrena
python scripts/train_wakeword.py --phrase "Oye Chat" --models data/models
```

Tarda unos minutos. Sintetiza la frase en más de cien voces, la mezcla con
ruido, ganancia y una reflexión de sala, y entrena el clasificador que
openWakeWord pone encima de su extractor de características. Los negativos son
frases de clase, casi-aciertos («oye», «chat», «choque») y habla variada, con
una segunda ronda que reentrena sobre los negativos que el modelo falla.

**Ese modelo aprende a reconocer voces sintéticas.** Sirve para levantar la
cadena entera y comprobar que funciona; no para llevarlo a un aula. Antes de
usarlo de verdad, añade grabaciones de personas reales:

```bash
python scripts/train_wakeword.py --phrase "Oye Chat" \
    --extra-positives grabaciones/frase \
    --extra-negatives grabaciones/aula
```

Treinta segundos de unas pocas voces reales valen más que mil clips
sintéticos.

## Estructura

```
backend/     motor local en Python: audio, detector, diagnóstico, estados
frontend/    interfaz React + TypeScript
src-tauri/   contenedor de escritorio en Rust
scripts/     construcción portable y entrenamiento del detector
docs/        especificación, decisiones técnicas y guía de antivirus
```

## Privacidad

Durante la escucha pasiva no sale audio del equipo: la palabra de activación se
detecta en local y los fragmentos se descartan según se procesan. No se guarda
audio, y las transcripciones solo se conservan si el profesor lo activa
(spec §14).

La clave de la API se guarda cifrada y nunca llega a la interfaz. Por defecto se
cifra con la cuenta de Windows del equipo, sin permisos de administrador (D-07).
Si marcas «poder usar esta clave en otros ordenadores», se cifra con una
contraseña que eliges tú (scrypt + AES-GCM) y entonces la carpeta funciona en
cualquier equipo del centro, a cambio de escribir la contraseña al empezar cada
sesión.

## Cómo se defiende de los falsos positivos

Que el asistente se despierte solo en mitad de una explicación es el fallo que
arruinaría la clase, así que hay tres defensas encadenadas:

1. **Filtro de voz.** El modelo VAD de Silero anula cualquier activación que no
   coincida con una persona hablando. Sillas, puertas y ventiladores quedan
   fuera por construcción.
2. **Confirmación por frames.** Se exigen dos frames consecutivos por encima
   del umbral: un pico de 80 ms no es una frase.
3. **Guarda de eco.** Mientras el asistente habla, el listón sube, para que no
   se confunda su propia voz saliendo por los altavoces con una interrupción.

Medido sobre ruido con el motor real, a sensibilidad extrema: **60 falsos por
hora sin filtro de voz, 0 con él**. Las tres son configurables y desactivables,
precisamente para poder medir cuánto aporta cada una.

### Medir en tu aula

Graba una hora de clase sin decir la frase, y unos cuantos clips diciéndola:

```bash
python scripts/measure_wakeword.py --models data/models \
    --negatives grabaciones/aula --positives grabaciones/frase
```

Devuelve, para cada sensibilidad, cuántas veces se habría despertado solo y
cuántas veces habría respondido cuando tocaba. Objetivo razonable: 0 falsos por
hora con detección por encima del 90%.

## Riesgos

El estado de cada uno está en
[`docs/decisiones-tecnicas.md`](docs/decisiones-tecnicas.md). Los siete que
había abiertos están mitigados o resueltos; lo que queda son dos medidas que
solo pueden tomarse en el instituto (falsos positivos reales y latencia de la
red del centro) y una prueba de antivirus, que conviene hacer con antelación y
no el día de la clase: ver [`docs/antivirus.md`](docs/antivirus.md).
