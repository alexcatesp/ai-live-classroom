# Plan de la Fase 1 — MVP

Fase 1 de la spec §20: carga de materiales, sesión, detector de activación,
pregunta-respuesta oral y registro básico. Al terminar deben cumplirse los
criterios de aceptación del MVP (spec §21).

Este plan parte de lo que ya existe tras la Fase 0: aplicación portable,
diagnóstico, máquina de estados completa (spec §7), detector «Oye Chat» con
entrenamiento con la voz del profesor (D-12), medidor de micrófono y
comprobación de la conexión Realtime (D-03).

---

## 1. Qué cambia para el profesor

Hoy: dice «Oye Chat», el estado pasa a *Activado* dos segundos y vuelve a
escuchar.

Al terminar la Fase 1:

1. Antes de clase carga sus materiales (PDF, PPTX, DOCX, TXT, Markdown).
2. Pulsa «Iniciar clase». La aplicación escucha en local, sin enviar nada.
3. Alguien dice «Oye Chat, ¿qué diferencia hay entre GET y POST?».
4. La aplicación captura la pregunta, la envía, y responde en voz en unos
   segundos, apoyándose en los materiales cuando vienen al caso.
5. La pregunta y la respuesta aparecen escritas en un panel que se puede
   ocultar (D-11).
6. Si alguien vuelve a decir «Oye Chat» o pulsa «Parar», la respuesta se corta
   al instante.
7. Vuelve a la escucha pasiva. Al finalizar queda un registro de la sesión.

---

## 2. Arquitectura del turno hablado

```
 micrófono 16 kHz ──► detector local ──► «Oye Chat»
        │                                     │
        │ (búfer circular ~1,5 s)             ▼
        │                          ACTIVATED → CAPTURING_REQUEST
        │                                     │
        └───── remuestreo 16→24 kHz ─────────►│ input_audio_buffer.append
                                              │   (solo desde la activación)
                                              ▼
                              fin de turno (VAD del servidor)
                                              │
                                              ▼ THINKING
                         response.output_audio.delta  (PCM 24 kHz, base64)
                         response.output_audio_transcript.delta
                                              │
                                              ▼ SPEAKING
                              reproducción en streaming (sounddevice)
                                              │
                          response.done ──────┴──► PASSIVE_LISTENING
```

Todo sigue en el backend Python (D-05, D-06): la clave no sale de él y el
frontend solo recibe estados y texto.

### Decisiones técnicas de partida

| Tema | Propuesta | Por qué |
|---|---|---|
| Sesión Realtime | **Una sesión caliente por clase**, abierta al pulsar «Iniciar clase», con reconexión automática | Abrir una por pregunta suma ~1,7 s de red medidos (R-2). Hay que verificar la duración máxima de una sesión y reconectar antes de que expire |
| Qué audio sale | Solo desde la activación hasta el fin de turno | spec §14 y §16: nada durante la escucha pasiva |
| Inicio de la pregunta | Búfer circular local de ~1,5 s; se envía desde el final de «Oye Chat» | La gente dice «Oye Chat, ¿qué…?» sin pausa; sin búfer se pierde el principio |
| Fin de turno | VAD del servidor (`server_vad` o `semantic_vad`), con `create_response: true` e `interrupt_response: false` | La interrupción la decide la aplicación, no cualquier ruido del aula |
| Silencio tras «Oye Chat» | Si en ~5 s no empieza una pregunta, `ACTIVATION_EXPIRED` (ya existe, P-13) | Una activación sin pregunta no debe dejar el micrófono enviando |
| Formato | PCM 16 bits, 24 kHz, mono, en ambos sentidos | Formato nativo de la API; el detector sigue a 16 kHz |
| Transcripción | Activar la transcripción de la entrada en la sesión | Necesaria para el panel (D-11) |
| Duración de la respuesta | Instrucciones (10–20 s, ampliada 20–45 s) más un tope de tokens configurable | spec §12 y §18 «duración máxima de respuesta» |
| Nombres de modelo y precios | En configuración, nunca fijados en el código | spec §16 |

Los nombres de eventos de esta sección son los de la documentación actual de
la API Realtime (versión GA). Hay que verificarlos al implementar.

---

## 3. Hitos

Ordenados por riesgo: primero lo que puede invalidar el enfoque.

### H1 — Cliente Realtime en streaming

- Ampliar `RealtimeClient` (hoy solo tiene `check_connection`) con una sesión
  persistente: `session.update` (instrucciones, voz, formato, transcripción,
  detección de turno), envío de audio, recepción de eventos y reconexión.
- **Servidor Realtime falso** para los tests: un WebSocket local que reproduce
  guiones de eventos (respuesta normal, error, corte de red, sesión expirada).
  Es lo que permite probar todo lo demás sin gastar ni depender de la red.
- Remuestreo 16→24 kHz.

**Terminado cuando** una conversación guionada pasa de extremo a extremo contra
el servidor falso, y una frase real grabada recibe una respuesta de la API real
en un script de prueba manual.

### H2 — Reproducción en streaming

- Ampliar `AudioEngine` con reproducción por bloques: cola de PCM que se
  alimenta según llegan los deltas, parada inmediata y posición reproducida en
  milisegundos (hace falta para truncar, H4).
- La guarda de eco (R-6) ya usa `is_playing`; adaptarla al streaming.

**Terminado cuando** un audio de 30 s llega en trozos, suena sin cortes y se
detiene en menos de 100 ms al pedirlo.

### H3 — Orquestación del turno

- Un `TurnController` que recorre los estados que ya existen:
  `ACTIVATED → CAPTURING_REQUEST → THINKING → SPEAKING → PASSIVE_LISTENING`.
  La máquina de estados no necesita cambios de fondo, solo los eventos.
- Búfer circular de preroll, tiempo máximo sin pregunta y tiempo máximo de
  pregunta.
- En Fase 0, `ACTIVATION_EXPIRED` se lanzaba siempre a los 2 s; ahora solo si
  no llega una pregunta.
- **Primer hito visible:** «Oye Chat, ¿qué es HTML?» recibe respuesta hablada.

**Terminado cuando** se cumple «Responde oralmente a una pregunta» (§21) en el
PC de desarrollo, y el test de privacidad demuestra que no sale ni un byte de
audio fuera de la ventana activación → fin de turno.

### H4 — Interrupción

- Disparadores:
  - «Oye Chat» durante *Pensando* o *Hablando*. El listener ya lo detecta y
    lanza `INTERRUPT`, con la guarda de eco.
  - Botón **Parar** en la interfaz.
- Acciones: detener la reproducción al instante, `response.cancel`,
  `conversation.item.truncate` con los milisegundos realmente reproducidos
  (así el modelo no «cree» que dijo lo que nadie oyó), y volver a escuchar o
  capturar la nueva pregunta.

**Terminado cuando** se cumple «El usuario puede interrumpir la respuesta»
(§21), con altavoces y no solo con auriculares (prueba de §22).

### H5 — Personalidad y panel de transcripción

- Prompt base de spec §13 como instrucciones de sesión, en un archivo editable
  y no en el código.
- Panel de transcripción (D-11): pregunta y respuesta en vivo, ocultable, sin
  guardar salvo que el profesor lo active (spec §14).
- Configuración (spec §18): voz de salida, duración máxima de respuesta y
  modelo.

### H6 — Materiales de la sesión

- Pantalla de preparación: añadir y quitar archivos; el estado *PREPARING* ya
  existe para esto.
- Extracción de texto por formato: PDF, PPTX con número de diapositiva, DOCX,
  TXT y MD. Un documento ilegible se notifica sin romper la sesión (§19).
- Uso en las respuestas: **ver la decisión abierta 2**.
- Guardado en `data/materials/` por sesión.

**Terminado cuando** se cumplen «Permite cargar PDF, PPTX, DOCX, TXT y
Markdown» y «La respuesta utiliza los materiales cargados cuando son
relevantes» (§21).

### H7 — Registro de la sesión, costes y errores

- SQLite (decisión de §23) en `data/sessions/`: sesión, turnos, duración, latencia
  hasta el primer audio, interrupciones y errores (spec §15).
- Consumo: guardar el `usage` que devuelve `response.done` y estimar el coste
  con precios en configuración (§16). Límite por sesión con aviso al acercarse.
- Errores de §19 que aún faltan: pérdida de conexión en mitad de un turno,
  clave caducada, consumo máximo alcanzado y respuesta demasiado larga. El
  estado nunca debe decir «escuchando» cuando no lo está.
- Borrar una sesión completa (§14).

### H8 — Validación del MVP

- Recorrer los doce criterios de §21 uno a uno y anotar el resultado.
- Pruebas de §22 en el instituto: micrófono integrado y USB, altavoces y
  auriculares, aula silenciosa y ruidosa, varias voces, interrupción, sin red y
  recuperación.
- Medir la latencia real del turno completo (R-2) y decidir si WebSocket basta.

---

## 4. Decisiones abiertas

Conviene cerrarlas antes de empezar el hito donde aparecen.

**1. Qué interrumpe una respuesta (H4).**
Recomendado: solo «Oye Chat» y el botón «Parar». La alternativa es que
cualquier voz interrumpa (`interrupt_response` del servidor), que es lo natural
en una conversación de dos pero en un aula cortaría la respuesta con cada
murmullo. La spec §6.2 dice «nueva intervención» sin concretar.

**2. Cómo usar los materiales antes del RAG (H6).**
El criterio de §21 exige usarlos en el MVP, pero el RAG es de la Fase 2.
Opciones:

- **a) Contexto directo** (recomendado para empezar): si el texto de la sesión
  cabe en un presupuesto de tokens, va en las instrucciones de la sesión. Es
  sencillo y suficiente para los apuntes de una clase. Si no cabe, se avisa al
  preparar la sesión.
- **b) Búsqueda por palabras clave** (BM25) sobre fragmentos, sin embeddings.
  Admite materiales grandes sin adelantar el modelo local de D-08.
- **c) Adelantar el RAG con embeddings** a la Fase 1: más trabajo y más peso en
  la carpeta portable.

**3. Sesión Realtime persistente o por pregunta (H1).**
Recomendado: persistente, por la latencia medida. Hay que confirmar que una
sesión abierta sin actividad no consume, y conocer su duración máxima.

**4. Guardar transcripciones (H5, H7).**
La spec dice «solo si el profesor lo activa». Propuesta: desactivado por
defecto y una casilla por sesión.

**5. Presupuesto por sesión (H7).**
Hace falta una cifra de partida y los precios vigentes del modelo en la cuenta
del centro.

---

## 5. Riesgos de la fase

| Riesgo | Mitigación |
|---|---|
| Latencia total del turno demasiado alta en el aula (R-2) | Sesión caliente, preroll local y medida del primer audio desde H3. Si no basta, WebRTC con token efímero (D-06) |
| El asistente se oye a sí mismo al hablar (R-6) | Guarda de eco ya existente, micrófono no enviado durante *Hablando* e interrupción solo por «Oye Chat» |
| El VAD del servidor corta preguntas con pausas para pensar | Probar `semantic_vad` frente a `server_vad` y ajustar el silencio mínimo |
| Sesiones que expiran en mitad de la clase | Reconexión transparente antes de expirar; la conversación previa no es crítica en Fase 1 |
| Coste descontrolado | Tope por respuesta, límite por sesión y registro del consumo desde H7 |
| Inspección TLS en el centro (R-3, R-8) | Ya detectada en el diagnóstico; el cliente en streaming reutiliza la misma verificación |
| Extracción mala de PPTX o PDF escaneados | Avisar por documento; el OCR queda fuera de la Fase 1 |

---

## 6. Orden de trabajo y entregas

Cada hito termina en una carpeta portable del CI que se puede probar.

1. **H1 + H2**: la base técnica, sin cambios visibles.
2. **H3**: primera respuesta hablada. **Primera prueba en aula recomendada.**
3. **H4**: interrupción.
4. **H5**: personalidad y transcripción.
5. **H6**: materiales.
6. **H7**: registro, costes y errores.
7. **H8**: validación del MVP en el instituto.

Al cerrar cada hito se actualizan este plan, `docs/decisiones-tecnicas.md` y el
README.
