# Decisiones técnicas — AI Classroom Live

Registro de decisiones (spec §24: "Registrar decisiones técnicas y problemas encontrados").
Fuente: `AI_Classroom_Live_Especificacion_Tecnica.docx`, versión 1.0.

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

## Problemas y riesgos abiertos

| # | Riesgo | Estado |
|---|--------|--------|
| R-1 | Falsos positivos de "Oye Chat" con ruido de aula y varias voces | Sin medir (D-04) |
| R-2 | Latencia del turno oral con WebSocket en lugar de WebRTC | Sin medir (D-06) |
| R-3 | Filtrado de WebSocket o inspección TLS en la red del instituto | Lo comprueba el diagnóstico (D-03) |
| R-4 | Peso de la carpeta portable con el modelo de embeddings | A vigilar (D-08) |
| R-5 | Reintroducir la clave al mover la carpeta entre equipos | Aceptado (D-07) |
| R-6 | Ausencia de cancelación de eco al reproducir y escuchar a la vez | Pendiente de Fase 1 (D-05) |
