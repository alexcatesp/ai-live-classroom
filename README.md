# AI Classroom Live

Asistente oral para el aula: permanece en silencio durante la clase y solo
interviene cuando alguien dice **«Oye Chat»**. Aplicación de escritorio portable
para Windows, pensada para ejecutarse en los equipos de un instituto sin
permisos de administrador.

> **Estado: Fase 1 (MVP) en desarrollo.** La Fase 0 está terminada:
> diagnóstico, palabra de activación y carpeta portable. Ya hay conversación
> hablada con «Oye Chat» (hitos H1 a H3 de
> [`docs/plan-fase-1.md`](docs/plan-fase-1.md)). Faltan los materiales, la
> personalidad, el registro de sesiones y la validación en el aula. Ver
> [`docs/decisiones-tecnicas.md`](docs/decisiones-tecnicas.md).

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
cita por apartado («spec §14», «spec section 7»). El plan de la siguiente fase,
con sus hitos y decisiones abiertas, está en
[`docs/plan-fase-1.md`](docs/plan-fase-1.md).

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
4. Abre **Configuración** con la rueda dentada de la esquina superior derecha,
   introduce la clave de la API y elige micrófono y altavoces. Un punto rojo en
   la rueda avisa de que falta la clave o de que hay que desbloquearla.
5. Pulsa **Comprobar equipo** (primer panel) y resuelve lo que salga en rojo.
   Cuando todo esté bien puedes plegar el panel con la flecha; se vuelve a
   abrir solo si una comprobación impide empezar.
6. En el panel de estado, **Preparar sesión** → **Iniciar clase**, y di «Oye Chat». El estado debe
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
no para fiarse de él en clase. Antes de usarlo de verdad, entrénalo con tu voz
desde la propia aplicación:

1. Con la clase pausada o sin empezar, abre **Entrenar con mi voz** en el panel
   de la palabra de activación.
2. Graba cinco veces «Oye Chat», una vez cada frase parecida que se ofrece y
   medio minuto hablando con normalidad. Al pulsar **Grabar** una frase tienes
   tres segundos; si la toma no vale, te dice por qué.
3. Pulsa **Entrenar**, que ajusta el modelo original a tu voz en menos de un
   minuto. Verás el modelo ajustado frente al original sobre el mismo audio, con
   una recomendación. Si te convence, pulsa **Usar este modelo**; se aplica al
   iniciar la siguiente clase.

Las grabaciones solo existen en memoria y se borran al terminar el
entrenamiento. **Volver al modelo original** deshace el cambio (D-12).

**Con la clase iniciada, el asistente responde.** Di «Oye Chat» y, sin pausa o
tras una breve, la pregunta. Termina sola cuando dejas de hablar un par de
segundos; la respuesta suena mientras llega y la aplicación vuelve a la escucha
pasiva.

- En el panel de estado ves la pregunta y la respuesta en texto, y si la
  conexión con la IA está disponible.
- Para cortar una respuesta, di «Oye Chat» o pulsa **Parar**.
- Al iniciar la clase se abre la conexión con la API. Sin clave guardada, la
  clase no empieza y dice por qué. Sin red, la clase empieza igualmente, escucha
  y avisa en cada activación mientras reintenta.
- Solo sale audio del equipo desde «Oye Chat» hasta el final de la pregunta.
  Durante la escucha pasiva no se envía nada.

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

Requisitos en la máquina de desarrollo (no en el equipo del aula): Python 3.11
(la versión que se distribuye; con 3.13 el diagnóstico TLS puede fallar en local
tras un antivirus que inspeccione HTTPS, ver R-8),
Node.js 20+, Rust estable y, en Linux, `libwebkit2gtk-4.1-dev libgtk-3-dev`.

```bash
# Backend
cd backend
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # 353 tests; los que necesitan modelos o audio real se omiten
.venv/bin/python -m ruff check src tests

# Frontend
cd frontend
npm ci
npm run test                            # 129 tests
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

### Probar la conversación con la API real (Fase 1, H1)

La conversación todavía no empieza con «Oye Chat», pero puede probarse desde la
propia aplicación: abre **Probar conversación**, pulsa **Hacer una pregunta** y
habla. Usa el micrófono y la clave de la configuración. La pregunta termina sola
tras un par de segundos de silencio, y el panel muestra lo que entendió, lo que
respondió, cuánto tardó en abrir la sesión y en empezar a sonar la respuesta,
y el consumo. La respuesta se reproduce por los altavoces
configurados; ahora suena mientras llega, y se puede parar al instante. No se guarda la pregunta; la respuesta queda en memoria hasta la
siguiente prueba.

También hay un script, sin interfaz, con una pregunta grabada en WAV:

```bash
export OPENAI_API_KEY=sk-...          # en PowerShell: $env:OPENAI_API_KEY="sk-..."
python scripts/realtime_smoke.py pregunta.wav --output respuesta.wav
```

Imprime la transcripción de la pregunta y de la respuesta, el tiempo hasta el
primer audio y el consumo, y guarda la respuesta hablada. Cuesta unos céntimos
por ejecución. Con Python 3.13 detrás de un antivirus que inspeccione HTTPS,
añade `--relaxed-tls` (R-8).

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
