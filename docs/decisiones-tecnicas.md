# Decisiones técnicas — AI Classroom Live

Registro de decisiones (spec §24: "Registrar decisiones técnicas y problemas encontrados").
Fuente: [`especificacion-tecnica.md`](especificacion-tecnica.md), versión 1.0.

Cada decisión indica su estado. Las marcadas como **abierta** deben revisarse tras
probar en un PC real del instituto.

---

## D-01 — Alcance de la primera entrega: solo Fase 0

**Decisión.** Se implementa únicamente la Fase 0 (prueba técnica) de la spec §20.
Nada de RAG, materiales docentes ni conversación completa todavía.

**Motivo.** La spec §24 pide validar audio y ejecución portable antes de construir
la arquitectura completa. Las dos restricciones más duras (ejecución sin permisos
de administrador y detección fiable de la palabra clave en un aula) son las que
pueden invalidar el diseño, así que se atacan primero.

**Consecuencia.** Fases 1-5 quedan pendientes. El código debe dejar las fronteras
de módulo definidas (frontend, motor de audio, gestor de contexto, cliente de IA)
aunque solo una parte esté implementada, conforme a la spec §5.

---

## D-02 — Entregable de Fase 0

La Fase 0 se da por terminada cuando se cumplen estos tres puntos:

1. **Diagnóstico completo.** Pantalla que comprueba y muestra, comprobación a
   comprobación: micrófono, altavoces, resolución DNS, certificados HTTPS,
   alcance de la API y validez de la clave.
2. **Palabra clave "Oye Chat".** Detector local funcionando, adelantado desde la
   Fase 1 para poder medir falsos positivos cuanto antes (spec §22).
3. **Carpeta portable verificada.** Un zip que se copia y se ejecuta en un PC sin
   permisos de administrador, sin Python ni Node.js instalados, con la máquina de
   estados de la spec §7 visible y el README de construcción incluido.

**Fuera de Fase 0.** Conversación oral de ida y vuelta, carga de materiales,
RAG, resumen de sesión y visor de diapositivas.

---

## D-03 — Realtime en Fase 0: solo verificación de conexión

**Decisión.** El diagnóstico abre la sesión WebSocket con la API Realtime,
confirma el handshake, la validez de la clave y que el modelo está autorizado en
la cuenta, y cierra la sesión. No hay conversación hablada en Fase 0.

**Motivo.** Valida el riesgo de red del instituto (proxy, certificados, filtrado
de WebSocket) sin arrastrar a la Fase 0 la complejidad del audio bidireccional,
el eco y la cancelación.

**Consecuencia.** El turno oral completo (captura, streaming de respuesta,
interrupción) es trabajo de Fase 1.

---

## D-04 — Detección de palabra clave: openWakeWord

**Decisión.** Detector local basado en openWakeWord, con un modelo propio para
"Oye Chat".

**Motivo.** Licencia abierta sin restricción de uso en un centro educativo y
ejecución en CPU. La alternativa más fiable (Porcupine) ofrece licencia gratuita
solo para uso personal o no comercial, lo que un instituto puede no admitir.

**Riesgo abierto.** La calidad en español con ruido de aula y varias voces está
sin medir. El modelo se genera a partir de muestras sintetizadas con TTS, y la
tasa de falsos positivos debe medirse en el aula (spec §22). Si no resulta
aceptable, la alternativa es STT local en buffer corto (Vosk o Whisper con VAD).

**Consecuencia.** El detector se programa detrás de una interfaz
`WakeWordDetector`, de modo que cambiar de motor no obligue a tocar el resto del
motor de audio.

---

## D-05 — Capa de audio en Python (sounddevice / PortAudio)

**Decisión.** Captura del micrófono, escucha pasiva, gestión de buffers y
reproducción se implementan en el backend Python con sounddevice sobre PortAudio.

**Motivo.** Mantiene todo el motor de audio en un único módulo, comprobable con
tests sin abrir la interfaz, y en el mismo proceso que openWakeWord, evitando
copiar audio entre procesos. PortAudio se empaqueta con PyInstaller y no exige
permisos de administrador.

**Alternativas descartadas.** WebAudio en el WebView (el WebView de Windows pide
permiso de micrófono y complica la escucha pasiva permanente); cpal en Rust
(obligaría a cruzar el audio hacia Python para el detector).

**Consecuencia.** El frontend nunca toca el micrófono: solo muestra estado y
envía órdenes al backend.

---

## D-06 — Transporte con la API Realtime: WebSocket desde el backend

**Decisión.** La sesión Realtime la mantiene el backend Python por WebSocket,
enviando y recibiendo PCM.

**Motivo.** La clave de la API nunca sale del backend, conforme a la spec §4.2,
que prohíbe incluirla en el frontend o en JavaScript distribuido. Encaja además
con D-05: el audio ya está en Python.

**Coste asumido.** El eco, los buffers y la cancelación de la respuesta hay que
gestionarlos a mano, sin la cancelación de eco que regala el navegador.

**Riesgo abierto.** Si la latencia en aula no resulta aceptable, la alternativa es
WebRTC desde el frontend con un token efímero emitido por el backend. Por eso el
cliente se programa detrás de una interfaz `RealtimeClient`.

---

## D-07 — Clave de la API: fichero cifrado con DPAPI

**Decisión.** La clave se introduce desde la pantalla de configuración y se
guarda en `config/settings.json` cifrada con la DPAPI de Windows, en ámbito de
usuario (no requiere permisos de administrador).

**Motivo.** Cumple la spec §4.2 sin dejar la clave en claro en disco, que es lo
que ocurriría con un `.env` junto al ejecutable.

**Consecuencia aceptada.** El cifrado va ligado al usuario y al equipo: al copiar
la carpeta portable a otro PC o a otra cuenta, hay que volver a introducir la
clave. La carpeta portable sigue siendo copiable; lo que no viaja es el secreto.

**Nota.** En desarrollo sobre Linux, DPAPI no existe: el almacén de
configuración debe abstraerse para poder usar una implementación alternativa
fuera de Windows sin cambiar el resto del código.

---

## D-08 — Embeddings locales con modelo embebido

**Decisión.** Cuando llegue la Fase 2, los embeddings se generan en local con un
modelo multilingüe pequeño en ONNX. No se envía el texto de los materiales a la
nube.

**Motivo.** Coste cero por documento, funcionamiento sin conexión y coherencia
con el objetivo de la spec §2 de minimizar el envío de datos fuera del equipo.

**Coste asumido.** La carpeta portable crece de forma apreciable con el modelo.

**Consecuencia.** Aunque el RAG no entra hasta la Fase 2, el tamaño del modelo
debe tenerse en cuenta desde ya al diseñar el empaquetado de la Fase 0.

---

## D-09 — Construcción del portable en CI (GitHub Actions, windows-latest)

**Decisión.** Un workflow compila Tauri y el backend con PyInstaller sobre
`windows-latest` y publica la carpeta portable como artefacto descargable.

**Motivo.** El desarrollo ocurre en Linux y Tauri no permite compilar
cómodamente para Windows desde Linux. El CI produce el zip; la validación en un
PC del instituto la hace una persona, descargándolo y ejecutándolo.

**Consecuencia.** El audio y la palabra clave no pueden comprobarse de forma
automática: los tests del CI cubren extracción, máquina de estados y lógica pura,
mientras que las pruebas de la spec §22 son manuales y su resultado se anota en
este mismo documento.

---

## D-10 — Idioma del repositorio

**Decisión.** Identificadores, comentarios y mensajes de commit en inglés.
README, este registro de decisiones y todos los textos que ve el profesor en
pantalla, en español de España (spec §12).

---

## D-11 — Transcripción visible de cada intervención (requisito de Fase 1)

**Decisión.** La interfaz mostrará en un panel de texto la transcripción de cada
intervención: lo que se pregunta **después** de «Oye Chat» y la respuesta del
asistente, a medida que llegan.

**Motivo.** Surgió en la primera prueba real: quien habla necesita saber qué ha
entendido el asistente, sobre todo cuando responde algo inesperado. Permite
además comprobar en clase si el fallo está en el reconocimiento de la pregunta
o en la respuesta. La API Realtime devuelve el texto de la entrada y de la
salida, así que no añade otro servicio.

**Límites, por la spec §14.**

- **Nada de escucha pasiva.** Lo que se dice en clase antes de «Oye Chat» no se
  transcribe ni sale del equipo; el panel empieza con la activación.
- **En pantalla no es en disco.** El panel muestra la conversación de la sesión
  en curso. Guardarla sigue dependiendo de que el profesor lo active, y se
  borra al cerrar si no lo ha hecho.
- El panel debe poder ocultarse, porque el aula puede estar proyectando la
  pantalla del profesor.

**Consecuencia.** El cliente Realtime de Fase 1 debe pedir la transcripción de
la entrada de audio y reenviar al frontend los eventos de texto de la entrada y
la respuesta, además del audio.

---

## D-12 — Entrenar el detector con la voz del profesor, dentro de la aplicación

**Decisión.** Un acordeón «Entrenar con mi voz», dentro del panel de la palabra
de activación, graba **cinco veces «Oye Chat»** y **una vez cada frase
parecida** («Oye chico», «Oye Chechu», «Oye cat», «Oye, ¿qué tal?», «Chat») y
reentrena el detector en el propio equipo, en uno o dos minutos.

**Motivo.** En la primera prueba real el modelo sintético se activaba «a veces»
con la frase y también con «Oye Chechu», «Oye chico» y «Oye cat». Reentrenar con
el script exige una máquina de construcción; el profesor tiene que poder
hacerlo solo, en el aula y con su micrófono. Las frases parecidas se graban
porque son justo los falsos positivos observados: enseñan al detector la
confusión que estaba cometiendo.

**Cómo, sin espeak-ng en el aula.**

- El entrenamiento (aumentado, ventanas de *embeddings*, MLP con minería de
  negativos difíciles y exportación a ONNX) pasa del script al paquete
  `aiclassroom.voice.training`, y el script lo importa.
- El CI guarda el corpus sintético ya convertido en ventanas
  (`data/models/oye_chat.corpus.npz`, float16, unos 30 MB) junto al modelo.
- En el aula se cargan esas ventanas, se añaden las de las grabaciones del
  profesor multiplicadas por aumentado (40 copias de cada una, y la mitad de las
  frases precedidas por una de sus frases parecidas, para reconocerla en mitad
  de una oración) y se reentrena el clasificador.
- scikit-learn ya viajaba en el paquete (P-5); se añade `onnx`. El autotest
  `--require-training` entrena y exporta un modelo mínimo con el ejecutable ya
  empaquetado, para que un paquete sin esas piezas falle en el CI y no delante
  del profesor.

**Privacidad (spec §14).** Es la única excepción a «no se guarda audio», y se
decidió así:

- Las grabaciones **solo existen en memoria**: nunca se escriben a disco.
- **Se descartan al terminar el entrenamiento**, salga bien o mal. Reentrenar
  significa volver a grabar.
- No salen del equipo; nada de este módulo usa la red.
- El panel lo explica antes de pulsar el primer botón.

**Seguridad del cambio.** El modelo nuevo no sustituye a nada hasta que el
profesor pulsa «Usar este modelo», tras ver cuántas de sus tomas reconoce y con
cuántas frases parecidas se sigue activando. Se guarda en
`data/models/personal/`, y el modelo original nunca se sobrescribe: «Volver al
modelo original» borra un archivo. El diagnóstico dice cuál de los dos se usa.

**Límites conocidos.**

- Las cifras que ve el profesor son optimistas: sus grabaciones también se
  usaron para entrenar. La medida de verdad sigue siendo
  `scripts/measure_wakeword.py` sobre una clase grabada (R-1).
- Entrenado con una voz, reconocerá mejor esa voz. La spec prevé que también
  invoquen al asistente los alumnos, así que conviene grabar varias voces si se
  quiere que respondan a todos.
- No se puede grabar mientras la clase escucha: el micrófono está ocupado.

---

## Decisiones heredadas de la spec (§23), sin discusión

- Aplicación de escritorio para Windows, no aplicación web.
- Distribución principal portable; el instalador, si llega, será opcional.
- Tauri 2 como contenedor, React y TypeScript en la interfaz.
- Python para procesamiento documental y gestión de contexto.
- SQLite como persistencia.
- Detección automática de la diapositiva en PowerPoint, fuera del MVP.
- Sin agentes autónomos complejos en la primera versión.
- La aplicación no habla por iniciativa propia en el MVP.

---

## Problemas encontrados durante la Fase 0

Anotados conforme aparecieron, como pide la spec §24.

### P-1 — El ejecutable empaquetado no arrancaba

PyInstaller ejecuta el script de entrada como módulo de nivel superior, así que
los imports relativos de `aiclassroom/main.py` fallaban en cuanto se congelaba
el binario: funcionaba en desarrollo y se rompía al empaquetar. Resuelto con
`backend/entrypoint.py`, un envoltorio que importa el paquete correctamente.

Lección: el empaquetado hay que probarlo, no suponerlo. Por eso el CI ejecuta
ahora `--selftest` sobre el binario ya empaquetado.

### P-2 — La carpeta `data/` se resolvía en el sitio equivocado

En la distribución de la spec §17 el backend vive en `runtime/backend/`, dos
carpetas por debajo de la raíz que contiene `data/`. La primera versión subía
un solo nivel, con lo que la configuración del profesor habría acabado dentro
de `runtime/` y se habría perdido en la siguiente actualización.

Resuelto de dos formas a la vez: el shell pasa `--data-dir` explícitamente,
porque sabe desde dónde se le ha lanzado, y el cálculo de respaldo del backend
está fijado con un test (`test_the_portable_root_is_found_from_the_backend_executable`).
El CI además comprueba que la carpeta portable resuelve la ruta esperada.

### P-3 — PortAudio puede faltar en el paquete sin que nadie se entere

`sounddevice` carga PortAudio de una biblioteca que debe viajar dentro del
paquete. Si falta, la aplicación arranca con normalidad y solo falla al abrir
el micrófono, es decir, delante de los alumnos.

Resuelto con `--selftest --require-audio`, que el CI de Windows ejecuta sobre
el binario empaquetado: un build sin PortAudio falla ahí y no en el aula. Cero
dispositivos es una respuesta válida (el runner no tiene tarjeta de sonido);
una biblioteca ausente, no.

### P-4 — openWakeWord descargaba sus modelos en tiempo de ejecución

El detector necesita dos modelos compartidos además del de la frase: un
extractor de melspectrogramas y el modelo de *embeddings* de voz. openWakeWord
los descarga la primera vez que se ejecuta, lo que en un PC del instituto
significaría una descarga segundos antes de la clase, en una red que
probablemente la bloquee.

Resuelto descargándolos en la máquina de construcción
(`scripts/fetch_wakeword_runtime.py`) y pasándole al detector sus rutas de
forma explícita, de modo que la biblioteca no toca la red. El diagnóstico
comprueba que están presentes, y el CI verifica que viajan en la carpeta
portable.

La descarga arrastra además las palabras de activación preentrenadas en inglés
("alexa", "hey jarvis", "timer") y las copias en tflite, unos 17 MB que esta
aplicación nunca carga. El script las descarta: 19 MB pasan a 2,4 MB.

### P-5 — El tamaño de la carpeta portable

`openwakeword/__init__.py` importa su módulo de verificadores personalizados sin
condición alguna, y ese módulo requiere scipy y scikit-learn. Aunque la
aplicación nunca usa un verificador, ambos acaban en el paquete: con
onnxruntime y numpy suman la mayor parte de los **~204 MB** que ocupa el backend
empaquetado (medido en Linux; en Windows el orden de magnitud es el mismo).

Se ha recortado lo que sí es seguro quitar: el motor de TensorFlow Lite (el
detector usa ONNX por D-04) y todo el aparato de entrenamiento, que solo se
utiliza en la máquina de construcción. Reducir los 204 MB restantes exigiría
parchear openWakeWord, y no se ha hecho.

Esto alimenta el riesgo R-4: cuando en la Fase 2 entre el modelo de embeddings
local (D-08), la carpeta crecerá todavía más. Conviene medir el tiempo de copia
a un USB antes de darla por buena. El CI imprime el tamaño en cada build.

### P-6 — CORS: la aplicación no podía hablar consigo misma

Al ejecutar la pila entera por primera vez —backend empaquetado, interfaz
compilada, Chromium— **todas** las llamadas a la API fallaron. La ventana la
sirve Tauri desde su propio origen (`http://tauri.localhost` en Windows,
`tauri://localhost` en Linux y macOS) y la página llama a
`http://127.0.0.1:<puerto>`, que para el navegador es otro origen. Sin
cabeceras CORS, el *preflight* se rechaza y no pasa ni una petición.

Ningún test lo detectó porque `TestClient` de FastAPI no hace *preflight*: sus
peticiones no son de navegador. La aplicación habría llegado al instituto con
una ventana que abre, se queda en «Conectando con el motor local…» y no hace
absolutamente nada.

Resuelto con `CORSMiddleware` restringido por expresión regular a los orígenes
de Tauri y de desarrollo —cualquier página que el profesor tenga abierta no
pinta nada aquí— y con tests que comprueban el *preflight* aceptado y
rechazado.

Lección: lo que solo se prueba con dobles no está probado. Por eso existe ahora
`scripts/validate-phase0.sh`.

### P-7 — Un 204 con cabeceras de cuerpo

FastAPI etiquetaba las respuestas vacías (guardar y borrar la clave) como
`content-type: application/json`. Un 204 no lleva cuerpo, y Chromium aborta la
respuesta cuando la ve anunciar uno.

Corregido con `response_class=Response`. Investigado hasta el final: el
`fetch` de la página resuelve `{ok: true, status: 204}`, así que el aborto que
reportaba el navegador era ruido interno por un cuerpo vacío sin leer y no
llegaba a la interfaz. Aun así el 204 estaba mal formado y ahora no lo está.

### P-9 — El detector aprendió el atajo, no la frase

Entrenar el clasificador de «Oye Chat» costó siete rondas, y las tres primeras
fallaron por errores míos en el corpus, no por el enfoque. Vale la pena
anotarlos porque cualquiera que reentrene tropezará con los mismos.

**Etiquetado desplazado.** Cada fotograma de embeddings resume los 0,76 s
anteriores, así que una ventana de 16 fotogramas abarca 1,96 s. Colocaba la
frase a 1,3 s del inicio y la etiquetaba como final de ventana: imposible, no
hay ninguna ventana que termine ahí. Se perdían dos tercios de los positivos.

**Colocación aleatoria con etiqueta fija.** La función que montaba el clip lo
insertaba en una posición aleatoria, mientras yo etiquetaba como si estuviera
en la que había pedido. Las ventanas «positivas» apuntaban a instantes
arbitrarios y el modelo aprendía de ruido.

**El atajo.** Ya con el etiquetado correcto, el modelo puntuaba 1,000 con
«mesa», «el perro» y «ocho». No había aprendido la frase: había aprendido
*enunciado corto entre silencios*, porque todos los positivos tenían esa forma
y todos los negativos eran frases largas continuas. Se arregla sintetizando
negativos cortos colocados exactamente igual que los positivos, de modo que el
aislamiento no aporte información.

**Y el simétrico.** Con eso resuelto, dejó de reconocer la frase dentro de
habla continua, que es justo como la dice un profesor. Todos los positivos
tenían silencio delante. Se arregla poniendo voz delante de la mitad de ellos.

El aviso general: **la precisión por ventanas engaña**. El conjunto reservado
daba 0,09% de falsos mientras el detector en streaming producía 869 activaciones
por hora, porque una clase genera decenas de miles de ventanas. La única medida
que significa algo es la de `scripts/measure_wakeword.py` sobre audio continuo.

### P-8 — Tauri no compilaba sin el sidecar — *superado por P-10*

Mientras el backend se declaraba como sidecar (`bundle.externalBin`),
`tauri::generate_context!` exigía que existiera
`binaries/aiclassroom-backend-<triple>`, y el trabajo de Linux del CI tenía que
fabricar un archivo de relleno para pasar clippy.

Ya no aplica: P-10 eliminó el sidecar. El shell lanza el backend por ruta
explícita desde `runtime\backend\`, el shell compila sin backend empaquetado y
ni el CI ni `build-portable.ps1` copian nada a `src-tauri\binaries\`.

---

### P-10 — Doble clic y no se abre nada

El primer intento de ejecutar la carpeta portable en Windows no abrió ninguna
ventana. Tres fallos encadenados, y el tercero es el que lo hizo invisible.

**El mecanismo de sidecar de Tauri no encaja con este empaquetado.** `sidecar()`
busca un ejecutable suelto junto a la aplicación; el backend vive en
`runtime\backend\` porque PyInstaller en modo carpeta necesita su `_internal`
al lado. Nunca lo encontraba.

**Y aunque lo hubiera encontrado, no habría arrancado.** El CI copiaba solo el
`.exe` a `binaries\`, sin el `_internal` que necesita para ejecutarse. Las dos
rutas estaban rotas a la vez.

**El `?` cerraba la aplicación en silencio.** Al fallar el arranque del backend,
`setup` devolvía error, Tauri abortaba y no se creaba ninguna ventana. En una
compilación de *release* no hay consola, así que no quedaba ni un mensaje: doble
clic, nada, y nada que investigar. Es el peor fallo posible de los tres.

Resuelto lanzando el backend por ruta explícita desde `runtime\backend\`, con
alternativas para desarrollo, y sobre todo: **la ventana se abre siempre**. Si
el motor no arranca, la ventana aparece con el motivo, con los dos sitios donde
mirar —la carpeta incompleta y el antivirus— y el detalle queda en
`data\arranque.log`. Un problema que nadie puede ver es un problema que nadie
puede arreglar.

Lección: el CI verificaba que la carpeta se construye y que el backend arranca
por su cuenta, pero nada comprobaba que **el contenedor pudiera lanzar al
backend**. Ese salto solo aparece al hacer doble clic.

### P-11 — La ventana abre, pero con `ERR_CONNECTION_REFUSED`

Con P-10 resuelto, la ventana ya aparecía, pero en blanco con
`ERR_CONNECTION_REFUSED`. Arrancar el backend a mano no cambiaba nada, porque
el rechazo no venía del backend: venía de `http://localhost:1420`, el servidor
de desarrollo de Vite.

Tauri decide en compilación de dónde carga la interfaz. Sin la feature
`custom-protocol` compila en modo desarrollo y usa `devUrl`; con ella, sirve
`frontend/dist` embebido en el ejecutable. `cargo tauri build` la activa por su
cuenta, pero el CI y `build-portable.ps1` usan `cargo build --release` a secas,
así que el `.exe` distribuido buscaba un Vite que en el aula no existe.

Resuelto compilando con `--features custom-protocol` en ambos sitios, y con un
`compile_error!` en `main.rs` que rechaza un build de *release* en modo
desarrollo: el error pasa de la pantalla del profesor a la compilación.

De paso, `build-portable.ps1` seguía copiando el backend a
`src-tauri\binaries\` como sidecar, un resto de antes de P-10 que ya no usaba
nadie.

### P-12 — Sin forma de saber si el micrófono oía algo

En la primera clase de prueba se dijo «Oye Chat» y no pasó nada. La única barra
en pantalla era la puntuación del detector, que con el filtro de voz activo
está a cero hasta que se reconoce la frase: igual para un micrófono mudo que
para uno que oye pero no reconoce. La causa resultó ser el micrófono elegido.

Resuelto con una barra de nivel del micrófono (dBFS de los últimos 0,5 s) y el
indicador «Voz detectada» del filtro Silero, encima del detector. De arriba
abajo, el panel responde en orden: ¿llega sonido?, ¿es voz?, ¿es la frase? Solo
se guarda un número por frame, nunca el audio.

### P-13 — La primera activación era también la última

Con el micrófono correcto, «Oye Chat» activó el asistente una vez y nunca más.
La máquina de estados no tenía salida de *Activado* salvo pausar o finalizar:
en la spec la activación lleva a capturar la petición, y eso es Fase 1. Las
detecciones siguientes llegaban en un estado que no las admite y se descartaban
en silencio. El README llegaba a afirmar que «se activa y vuelve a silencio».

Los tests no lo vieron porque todos comprobaban **una** activación y terminaban
ahí.

Resuelto con el evento `ACTIVATION_EXPIRED` (*Activado* → escucha pasiva). En
Fase 0 el controlador lo lanza a los 2 s, para que la interfaz alcance a
mostrar el estado; en Fase 1 será el tiempo de espera de quien dice la frase y
luego nada. El temporizador no toma el cerrojo del controlador, porque se arma
desde el hilo de audio y `stop()` espera a ese hilo con el cerrojo tomado.

## Verificado en Windows real

El CI construyó la carpeta portable por primera vez el 13/09/2026
([run 34746600377](https://github.com/alexcatesp/ai-live-classroom/actions/runs/34746600377)).
El autotest del ejecutable ya empaquetado, ejecutado desde dentro de la carpeta:

```json
{"ok": true, "frozen": true, "python": "3.11.9",
 "audio_library": true, "audio_error": null,
 "audio_inputs": 0, "audio_outputs": 0,
 "wakeword_engine": true, "secret_store": true,
 "data_dir": "...\\release\\AI-Classroom-Live\\data"}
```

Cierra tres cosas que en Linux no podían comprobarse:

- **P-3 queda verificado**: `audio_library: true` significa que PortAudio viaja
  dentro del paquete en Windows. Cero dispositivos es lo normal en un runner y
  ya no hace fallar el build, porque se distingue de que falte la biblioteca.
- **P-1 y P-2 quedan verificados**: el ejecutable arranca congelado y resuelve
  `data/` dentro de la carpeta portable, no en `runtime/`.
- `wakeword_engine` y `secret_store` confirman que openWakeWord y el módulo
  nativo de cifrado sobreviven al empaquetado.

Carpeta resultante: 206 MB, 661 archivos con suma de verificación, 88 MB
comprimida. La compilación de Tauri en Windows tarda unos 6 minutos y medio.

Sigue sin comprobarse lo que necesita un equipo del instituto: abrir un
micrófono de verdad, hablar con la API y la reacción del antivirus del centro.

## Riesgos: estado

Todos los riesgos abiertos de la versión anterior se han atacado. Lo que queda
pendiente está acotado y es de medición, no de diseño.

### R-1 — Falsos positivos de «Oye Chat» — *mitigado, pendiente de medir en aula*

Tres defensas, de la más barata a la más cara:

1. **Puerta de actividad de voz.** El modelo VAD de Silero corre junto al
   detector y anula cualquier puntuación que no coincida con una persona
   hablando. Una silla, una puerta o el ventilador del proyector no pueden
   despertar al asistente, por mucho que el detector crea haber oído algo.
2. **Confirmación por frames.** Un solo frame por encima del umbral es un pico,
   no una frase. Se exigen 2 frames consecutivos (160 ms), configurable.
3. **Ventana refractaria**, que ya existía, para no contar dos veces la misma
   activación.

**Medido aquí** con el modelo entrenado y el motor real, sobre habla sintética
continua en español (frases de clase) y clips de la frase:

| Sensibilidad | Falsos por hora | Detección |
|---|---|---|
| 0,20 | **0** | 100% |
| 0,35 | **0** | 100% |
| 0,50 (predeterminada) | **0** | 100% |
| 0,65 | **0** | 100% |
| 0,80 | **0** | 100% |

Y sobre ruido con transientes, la puerta VAD por separado: 60 activaciones por
hora sin ella, 0 con ella.

Es voz sintética, sin acústica de aula ni varias personas hablando a la vez, de
modo que estos números son un suelo, no una predicción. Lo que demuestran es
que la cadena entera funciona y que las defensas hacen lo que dicen.

**Lo que falta** es un dato que solo existe en el instituto: graba una hora de
clase real sin decir la frase y pásala por el banco de medida. El modelo actual
está entrenado con voces sintéticas y reconoce voces sintéticas; una persona
real a cuatro metros de un portátil es otra cosa. Reentrena añadiendo
grabaciones con `--extra-positives` antes de usarlo en clase:

```bash
python scripts/measure_wakeword.py --models data/models \
    --negatives grabaciones/aula --positives grabaciones/frase
```

Devuelve falsos por hora y tasa de detección para cada sensibilidad. El objetivo
razonable es 0 falsos por hora con detección por encima del 90%. Anota aquí el
resultado.

### R-2 — Latencia con WebSocket en lugar de WebRTC — *instrumentado*

El diagnóstico mide el tiempo de establecimiento de la sesión Realtime, lo
muestra en pantalla y avisa por encima de 1,5 segundos. Cubre lo que aporta la
red del centro: DNS, TCP, TLS y el *upgrade*.

No es la latencia del turno hablado completo, que añade la del modelo y llega
con la Fase 1, pero es la parte que varía de un aula a otra y la que decide si
merece la pena cambiar de transporte. La decisión D-06 deja de depender de una
intuición: habrá un número.

### R-3 — Inspección TLS en la red del instituto — *detectado y nombrado*

Antes solo se detectaba cuando la cadena fallaba. Ahora el diagnóstico lee el
emisor del certificado: si no es una autoridad pública conocida, la cadena
valida pero alguien está leyendo el tráfico, y eso se dice con nombre y
apellidos.

Se avisa sin bloquear —la clase puede seguir— y se recomienda consultarlo con
el administrador antes de usar la aplicación con datos del alumnado.

Comprobado en vivo: el contenedor de desarrollo está tras un proxy que
inspecciona HTTPS, y la comprobación lo señaló por su nombre.

### R-4 — Peso de la carpeta portable — *cerrado*

Unos 204 MB. Descartado como problema por decisión expresa. La consecuencia es
que las defensas del detector pueden permitirse su coste en disco: el modelo
VAD son 1,7 MB que antes se habrían discutido.

### R-5 — Reintroducir la clave al mover la carpeta — *resuelto, opcional*

Hay un segundo almacén: scrypt deriva una clave de una contraseña que elige el
profesor y AES-GCM cifra con ella. El cifrado no depende de nada de la máquina,
así que la carpeta funciona en cualquier ordenador del centro.

DPAPI sigue siendo el predeterminado, porque cambia un secreto que guarda
Windows por uno que hay que recordar. Es una casilla en la pantalla de
configuración, con el coste de cada opción escrito al lado.

Los tokens llevan marcado el esquema que los escribió, de modo que un archivo
copiado entre equipos pide la contraseña en lugar de devolver basura. AES-GCM
autentica, así que un archivo manipulado no descifra.

### R-6 — Sin cancelación de eco — *mitigado*

Sin AEC, el micrófono oye los altavoces y el asistente puede oírse a sí mismo
decir la frase e interrumpir su propia respuesta.

Cerrar el micrófono mientras habla costaría la interrupción por voz que pide la
spec §6.2. En su lugar hay una guarda que sube el listón durante la
reproducción: una persona a un metro del micrófono suena más fuerte y más
limpia que el retorno de los altavoces. Cada activación descartada se cuenta y
se muestra, así que el margen se ajusta con datos.

La cancelación de eco de verdad llega si se adopta WebRTC (R-2), que la trae
del navegador. Hasta entonces, la guarda es la mitigación, y se puede
desactivar cuando se usan auriculares y no hay camino acústico que guardar.

### R-7 — Antivirus con un ejecutable sin firmar — *gestionado*

Tres medidas:

- El script de construcción **firma** los ejecutables si se le da un
  certificado (`-CertificatePath`), con sellado de tiempo.
- Sin certificado, cada construcción genera **`SHA256SUMS.txt`**, que es lo que
  necesita un administrador para conceder una excepción con fundamento.
- **`docs/antivirus.md`** explica por qué ocurre, qué pedir al administrador y
  cómo verificar la carpeta.

Sigue sin comprobarse en un equipo real del instituto, y esa prueba conviene
hacerla con antelación, no el día de la clase.

### R-8 — Python 3.13 rechaza los certificados de inspección HTTPS mal formados — *abierto, sin impacto hoy*

**Qué pasó.** Al pasar el diagnóstico con el backend en desarrollo, sobre
Python 3.13, «Certificados TLS» falló con *Basic Constraints of CA cert not
marked critical*. En ese PC, Avast Web/Mail Shield inspecciona el HTTPS: sustituye
el certificado de `api.openai.com` por uno emitido por su propia raíz, y esa raíz
no marca la extensión *basicConstraints* como crítica.

**Por qué.** Desde Python 3.13, `ssl.create_default_context()` activa
`VERIFY_X509_STRICT`, que exige que los certificados cumplan RFC 5280. Hasta la
3.12 esa comprobación no existe, y la cadena se aceptaba. Muchas raíces de
antivirus y de proxies corporativos no cumplen la norma.

**Situación actual.** La carpeta portable se construye con **Python 3.11**
(fijado en `ci.yml`), así que la aplicación que se distribuye no se ve
afectada. En el mismo PC, el ejecutable empaquetado dio el diagnóstico en verde.

**Qué supondría actualizar a 3.13 sin hacer nada.**

- En cualquier equipo con un antivirus o proxy así, «Certificados TLS» sale en
  rojo, la clave y la conexión Realtime no se comprueban y **la clase no puede
  empezar**.
- No afecta solo al diagnóstico: el cliente Realtime usa el mismo contexto por
  defecto, así que en la Fase 1 el asistente **no podría conectar**.
- En un instituto es probable: filtrado de contenidos con inspección HTTPS, o
  antivirus de consumo en portátiles del profesorado.

**Por qué no basta con fijar 3.11 para siempre.** Python 3.11 deja de recibir
parches de seguridad en octubre de 2027. Actualizar llegará.

**Mitigaciones, para decidir entonces.**

1. Crear el contexto TLS en un único sitio, compartido por diagnóstico y cliente
   Realtime, y desactivar solo `VERIFY_X509_STRICT`. Se mantienen la validación
   de la cadena y del nombre del servidor; se renuncia a la comprobación de
   conformidad que 3.13 añadió. Es lo que hacía Python hasta la 3.12.
2. Mantener el comportamiento estricto y pedir al administrador que excluya la
   aplicación de la inspección HTTPS. Es más limpio, pero depende de terceros
   en cada centro.
3. En cualquier caso, mejorar el mensaje del diagnóstico: hoy atribuye el fallo
   a «la red del centro», y puede ser el antivirus del propio equipo. El emisor
   del certificado dice cuál de los dos es.

**Consecuencia para el desarrollo.** Quien trabaje con Python 3.13 en un PC con
un antivirus así verá el diagnóstico en rojo en local, sin que la aplicación
empaquetada tenga el problema. Conviene desarrollar con 3.11, la versión que se
distribuye.

---

## Problemas y riesgos abiertos

| # | Riesgo | Estado | Qué falta |
|---|--------|--------|-----------|
| R-1 | Falsos positivos de «Oye Chat» | 0/hora medidos sobre voz sintética | Reentrenar con voces reales y medir una clase |
| R-2 | Latencia del transporte WebSocket | Instrumentado y avisado | Leer el número en el instituto |
| R-3 | Inspección TLS en la red del centro | Detectado y nombrado | — |
| R-4 | Peso de la carpeta portable | Cerrado por decisión | — |
| R-5 | La clave no viaja con la carpeta | Resuelto (contraseña opcional) | — |
| R-6 | El asistente puede oírse a sí mismo | Mitigado con guarda de eco | AEC real llega con WebRTC |
| R-7 | Antivirus y ejecutable sin firmar | Firma opcional + sumas + guía | Probar en un equipo del centro |
| R-8 | Python 3.13 rechaza certificados de inspección mal formados | Sin impacto: se distribuye con 3.11 | Decidir la mitigación antes de actualizar Python |

Lo único que queda pendiente son dos medidas que solo pueden tomarse en el
instituto, y una prueba de antivirus. Ninguna es trabajo de diseño.
