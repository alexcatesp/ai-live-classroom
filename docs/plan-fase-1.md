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
| Fin de turno | **Automático, ~2 s después de dejar de oír voz** (configurable). VAD del servidor con `silence_duration_ms` ≈ 2000, `create_response: true` e `interrupt_response: false` | Decidido con el profesor: aula de FP de grado superior, tranquila y con micrófono direccional. Dos segundos toleran una pausa para pensar a cambio de sumarlos a la espera de la respuesta |
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

**Estado (13/09/2026): terminado.** Probado con la API real desde la aplicación:
0,84 s para abrir la sesión y 0,45 s desde el fin de la pregunta hasta el primer
audio. Las medidas y sus conclusiones están en R-2. Dos tareas para H5: la
respuesta duró 31 s (el objetivo son 10–20 s) y contenía una imprecisión menor.

- `realtime/session.py`: `RealtimeConnection` (un WebSocket configurado) y
  `ManagedRealtimeSession` (la sesión de toda la clase). Incluye reconexión con
  espera creciente, renovación entre turnos antes de una edad configurable
  (50 min), y abandono inmediato si la clave o el modelo se rechazan.
- `realtime/events.py`: construcción de los eventos del cliente y
  normalización de los del servidor.
- `realtime/audio.py`: remuestreo 16→24 kHz en streaming, idéntico a remuestrear
  todo de una vez.
- `tests/fake_realtime.py`: servidor falso con respuesta guionada, cancelación,
  borrado de elementos, clave rechazada, configuración rechazada y cortes.
- `scripts/realtime_smoke.py`: envía un WAV grabado a la API real e imprime la
  transcripción, el tiempo hasta el primer audio y el consumo.
- **Panel «Probar conversación»** (`realtime/probe.py`): lo mismo desde la
  aplicación, con el micrófono y la clave de la configuración. La pregunta viaja
  en streaming y la da por terminada el servidor, como en clase. El micrófono se
  cierra antes de que llegue la respuesta, y un test lo comprueba. La respuesta
  se puede escuchar a 48 kHz por los altavoces configurados. Adelanta a H1 una
  reproducción simple (el audio completo, sin streaming) que H2 sustituirá.
- Configuración nueva: `turn_silence_ms` (2000), `transcription_model`,
  `realtime_noise_reduction` y `history_turns` (4).

Lo aprendido de la documentación al empezar:

- **Una sesión abierta sin actividad no consume**: se cobra por tokens.
  Decisión 3 confirmada: sesión persistente.
- **El historial cuesta.** Cada pregunta y respuesta anteriores se vuelven a
  facturar como entrada en cada turno nuevo. Por eso solo se conservan los
  últimos turnos (`history_turns`) y los anteriores se borran de la
  conversación con `conversation.item.delete`.
- **No se encontró documentada la duración máxima** de una sesión Realtime. Por
  eso se renueva a los 50 minutos, configurable, y siempre entre turnos.

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
  - Botón **Parar**, de emergencia: visible solo mientras el asistente piensa o
    habla. «Pausar» también corta la respuesta que esté sonando.
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

**1. Qué interrumpe una respuesta (H4). — Decidido.**
«Oye Chat» y un botón «Parar» de emergencia; «Pausar» también corta. La
pregunta termina sola unos 2 s después de dejar de oír voz. Si en el aula
resulta demasiado lento o corta preguntas, el silencio se ajusta en la
configuración.

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

**3. Sesión Realtime persistente o por pregunta (H1). — Decidido: persistente.**
Una sesión abierta sin actividad no consume. Como la duración máxima no está
documentada, se renueva entre turnos. El historial se limita para que el coste
no crezca con la clase.

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
| El fin de turno automático corta preguntas con pausas para pensar, o tarda demasiado | 2 s de silencio de partida, configurable; medir en el aula y comparar `server_vad` con `semantic_vad` |
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
