# AI Classroom Live — Especificación técnica

**Aplicación de escritorio portable para Windows**

| | |
|---|---|
| **Versión** | 1.0 |
| **Estado** | Propuesta técnica para implementación con Claude Code o Codex |
| **Formato** | Transcripción a Markdown del documento original en Word |

> **Sobre este documento.** Es el documento de partida, transcrito sin cambiar
> ningún requisito. La numeración de apartados se conserva porque el código la
> cita: encontrarás comentarios como «spec §14» o «spec section 7» a lo largo
> de todo el repositorio.
>
> Las **notas de implementación** marcadas así aparecen donde la especificación
> dejó algo abierto y ya se ha decidido. No forman parte de la versión 1.0: son
> punteros a [`decisiones-tecnicas.md`](decisiones-tecnicas.md), donde vive el
> razonamiento completo de cada decisión (D-xx), cada problema encontrado
> (P-xx) y cada riesgo (R-x).

---

## 1. Resumen ejecutivo

AI Classroom Live será una aplicación de escritorio para Windows que actuará
como un tercer participante en el aula. Permanecerá en modo silencioso mientras
se desarrolla la clase y solo intervendrá cuando detecte la frase de activación
**«Oye Chat»**. Tras la activación, capturará la pregunta, consultará los
materiales docentes y el contexto de la sesión, generará una respuesta oral y
volverá automáticamente al modo silencioso.

La aplicación debe ejecutarse en los ordenadores del instituto sin permisos de
administrador, sin instalación global, sin servicios de Windows y sin depender
de software previamente instalado, salvo los controladores de audio y la
conectividad disponibles en Windows.

## 2. Objetivos

- Responder oralmente a preguntas espontáneas del profesor o del alumnado.
- Ampliar explicaciones del módulo de Inteligencia Artificial para el
  Desarrollo Web.
- Participar en debates cuando se le invoque explícitamente.
- Proponer ejemplos, analogías, preguntas de comprobación y pequeñas
  actividades.
- Utilizar los apuntes, presentaciones y documentos cargados antes de la clase.
- Mantener el contexto de lo explicado durante la sesión.
- Conocer, cuando sea posible, la diapositiva o sección que se está tratando.
- No interrumpir la clase por iniciativa propia en el MVP.
- Minimizar el envío de audio y datos fuera del equipo.

## 3. Experiencia de usuario

1. Antes de la clase, el profesor crea una sesión y carga sus materiales.
2. El profesor comprueba micrófono, altavoces y conexión.
3. Al comenzar, pulsa únicamente **«Iniciar clase»**.
4. Durante la clase, la aplicación permanece en escucha pasiva local.
5. Para invocarla, el profesor o un alumno dice: «Oye Chat, …».
6. La aplicación captura la petición, responde oralmente y vuelve al modo
   silencioso.
7. El profesor puede pausar o finalizar la sesión desde la interfaz.

## 4. Restricciones del entorno del instituto

### 4.1. Ejecución

- Compatible inicialmente con Windows 10/11, sujeto a validación en equipos
  reales.
- Ejecución desde una carpeta del usuario, USB o recurso de red.
- Sin permisos de administrador.
- Sin escritura en `Program Files`.
- Sin modificación del registro.
- Sin servicios de Windows.
- Sin drivers de audio propios.
- Sin Docker en el equipo del instituto.
- Sin requerir Python, Node.js o .NET instalados previamente.
- Todos los runtimes necesarios deben distribuirse con la aplicación.

### 4.2. Red y seguridad

La aplicación necesitará conexión HTTPS con el proveedor de IA. Debe incluir un
diagnóstico que compruebe **conectividad, resolución DNS, certificados y
disponibilidad de la API** antes de iniciar la clase.

La clave de API no se incluirá en el frontend ni en código JavaScript
distribuido. Se almacenará mediante configuración local protegida o se
introducirá desde una pantalla de configuración.

> **Nota de implementación.** El diagnóstico también identifica al emisor del
> certificado, de modo que una red del centro que inspeccione HTTPS se nombra
> en lugar de deducirse (R-3). La clave se cifra con DPAPI en ámbito de usuario
> (D-07), con la contraseña como alternativa portable (R-5), y nunca aparece en
> ninguna respuesta que reciba el frontend.

## 5. Arquitectura tecnológica recomendada

| Pieza | Tecnología |
|---|---|
| Contenedor de escritorio | Tauri 2 |
| Interfaz | React + TypeScript |
| Backend local | Python empaquetado como ejecutable portable |
| Comunicación local | FastAPI sobre localhost o IPC equivalente |
| Persistencia | SQLite |
| Procesamiento documental | Librerías Python para PDF, DOCX, PPTX, TXT y Markdown |
| RAG local | Base vectorial embebida o almacenamiento vectorial local |
| Voz en tiempo real | API Realtime de OpenAI, usando el modelo disponible y autorizado en la cuenta |
| Detección de activación | Detector local de palabra clave |

El frontend, el motor de audio, el gestor de contexto y el cliente de IA deben
estar **desacoplados**. Esto permitirá sustituir Tauri por Electron o cambiar el
proveedor de IA sin reescribir toda la aplicación.

> **Nota de implementación.** El detector local es openWakeWord (D-04); todo el
> audio vive en Python (D-05); el transporte con Realtime es WebSocket desde el
> backend (D-06). Cada pieza está detrás de una interfaz con su doble de
> prueba, que es lo que permite ejecutar la suite sin tarjeta de sonido.

## 6. Componentes del sistema

### 6.1. Aplicación de escritorio

- Ventana principal de preparación y control de sesión.
- Modo minimizado o discreto durante la clase.
- **Indicador visible del estado del micrófono.**
- Indicadores de escucha pasiva, activación, procesamiento y respuesta.
- Controles de iniciar, pausar y finalizar.
- Configuración de micrófono, altavoces, voz, modelo y carpeta de datos.

### 6.2. Motor de audio

- Captura del micrófono mediante dispositivos reconocidos por Windows.
- Escucha pasiva local para detectar «Oye Chat».
- Captura de la intervención posterior a la palabra clave.
- Detección del final de turno mediante silencio o señal del motor de voz.
- Envío del audio relevante al motor Realtime.
- Reproducción del audio de respuesta.
- **Cancelación inmediata de la respuesta cuando se detecte una nueva
  intervención.**

### 6.3. Gestor de contexto

El gestor combinará:

- Instrucciones permanentes del asistente.
- Materiales docentes de la sesión.
- Diapositiva o sección actual, si está disponible.
- Resumen acumulado de la clase.
- Últimas intervenciones y respuestas.
- Pregunta o petición actual.

## 7. Máquina de estados

| Estado | Significado |
|---|---|
| `IDLE` | Aplicación cerrada o sin sesión |
| `PREPARING` | Selección de materiales y configuración |
| `READY` | Sesión preparada |
| `PASSIVE_LISTENING` | Escucha local de la palabra clave |
| `ACTIVATED` | Se ha detectado «Oye Chat» |
| `CAPTURING_REQUEST` | Captura de la pregunta |
| `THINKING` | Recuperación de contexto y generación de respuesta |
| `SPEAKING` | Reproducción de la respuesta |
| `INTERRUPTED` | Respuesta cancelada por una nueva intervención |
| `PAUSED` | Sesión temporalmente detenida |
| `STOPPED` | Sesión finalizada |
| `ERROR` | Fallo recuperable o bloqueante |

> **Nota de implementación.** El estado del micrófono se deriva del estado de la
> sesión en lugar de llevarse aparte, de modo que ningún camino de error puede
> dejar el indicador diciendo que escucha cuando no lo hace (§19). Está en
> `backend/src/aiclassroom/session/state.py`.

## 8. Flujo funcional principal

1. El profesor crea una sesión.
2. Carga los documentos y, opcionalmente, una presentación.
3. La aplicación extrae texto, estructura y metadatos.
4. Se generan fragmentos y embeddings para búsquedas contextuales.
5. El profesor comprueba micrófono, altavoces y conexión.
6. Pulsa «Iniciar clase».
7. El sistema entra en escucha pasiva local.
8. Se detecta «Oye Chat».
9. Se captura la petición completa.
10. Se recuperan los fragmentos relevantes y el contexto reciente.
11. Se construye la solicitud al modelo Realtime.
12. La respuesta se reproduce por voz en streaming.
13. Si alguien interrumpe, se cancela la respuesta.
14. El sistema vuelve a escucha pasiva.
15. Al finalizar, se guarda el resumen y las métricas de la sesión.

## 9. Materiales docentes

**Formatos iniciales:** PDF, PPTX, DOCX, TXT y Markdown.

**Formatos opcionales posteriores:** páginas web, HTML, imágenes y enlaces.

Cada documento tendrá metadatos como nombre, tipo, fecha de incorporación,
número de página o diapositiva y posición dentro del documento. Las respuestas
podrán identificar internamente el material utilizado.

## 10. RAG local

1. Extraer el texto de los documentos.
2. Conservar títulos, apartados, listas, tablas simples y referencias de página.
3. Fragmentar el contenido en unidades semánticas.
4. Generar embeddings.
5. Guardar los vectores localmente.
6. Buscar fragmentos relevantes ante cada pregunta.
7. Incluir solo el contexto necesario.
8. Indicar claramente cuando la respuesta no se encuentra en los materiales.

> **Nota de implementación.** Los embeddings se generan en local con un modelo
> embebido, sin enviar el texto de los materiales fuera del equipo (D-08).

## 11. Gestión de diapositivas

Evolución prevista:

| Fase | Alcance |
|---|---|
| MVP | El profesor carga los materiales y puede indicar manualmente la diapositiva actual |
| Fase 2 | La aplicación muestra la presentación y permite avanzar o retroceder |
| Fase 3 | Integración opcional con PowerPoint para detectar la diapositiva activa |

La integración automática con PowerPoint **no será requisito del MVP**, ya que
puede verse limitada por las políticas de seguridad del instituto.

## 12. Personalidad y comportamiento

- Idioma predeterminado: **español de España**.
- Tono natural, cercano, didáctico y profesional.
- Adaptación al nivel de Formación Profesional.
- Respuestas directas, evitando monólogos innecesarios.
- Respuesta rápida habitual: **10–20 segundos**.
- Respuesta ampliada: aproximadamente **20–45 segundos** cuando proceda.
- Uso de ejemplos de programación y desarrollo web.
- No inventar datos ni afirmar que algo aparece en los materiales si no aparece.
- Reconocer la incertidumbre y pedir precisión cuando la pregunta sea ambigua.
- No decir repetidamente «como IA».
- No intervenir espontáneamente en el MVP.

## 13. Prompt base orientativo

> Eres AI Classroom Live, un asistente oral que participa en una clase de
> Formación Profesional. Solo debes responder cuando el sistema te active
> mediante la frase «Oye Chat». Hablas en español de España, con naturalidad,
> claridad y tono didáctico. Adapta las explicaciones al nivel del alumnado.
> Prioriza los materiales de la sesión y el contexto reciente. Si no tienes
> información suficiente, dilo claramente. Responde de forma concisa salvo que
> se solicite una explicación más profunda. Utiliza ejemplos concretos,
> especialmente relacionados con programación, datos, inteligencia artificial y
> desarrollo web. No interrumpas ni inventes intervenciones.

## 14. Privacidad y protección de datos

- No guardar audio bruto por defecto.
- **No enviar audio continuamente a la nube durante la escucha pasiva.**
- Guardar transcripciones solo si el profesor lo activa.
- Permitir borrar una sesión completa.
- Mostrar claramente cuándo el micrófono está activo.
- Separar datos de configuración, materiales, sesiones y métricas.
- No incluir nombres de alumnos salvo que sea estrictamente necesario.
- Incluir aviso de uso responsable y consentimiento conforme a la política del
  centro.

## 15. Observabilidad y métricas

- Hora de inicio y final de sesión.
- Número de activaciones.
- Duración de cada intervención.
- Latencia hasta el primer audio.
- Duración de la respuesta.
- Errores de red o de audio.
- Tokens o unidades de consumo comunicadas por la API.
- Estimación de coste por sesión.

## 16. Control de costes

- No enviar audio durante la escucha pasiva.
- Limitar el contexto recuperado.
- Resumir periódicamente el historial.
- Evitar repetir documentos completos en cada petición.
- Configurar un límite diario o por sesión.
- Mostrar una estimación de consumo.
- Permitir seleccionar un modelo más económico cuando sea suficiente.

Los precios y nombres exactos de los modelos deberán verificarse en la
documentación oficial y en la cuenta utilizada durante la implementación. **No
deben quedar fijados irreversiblemente en el código.**

## 17. Distribución portable

La entrega prevista será una carpeta autocontenida similar a:

```
AI-Classroom-Live.exe
runtime/backend.exe
resources/
models/
data/
config/
README.txt
```

El usuario debe poder copiar la carpeta y ejecutar el programa **sin
instalador**. Si se necesita una versión instalable, deberá ser opcional y no
sustituir a la versión portable.

> **Nota de implementación.** La disposición real es
> `runtime/backend/aiclassroom-backend.exe` porque PyInstaller en modo carpeta
> necesita su `_internal` al lado. Esa diferencia importa: causó dos fallos
> reales (P-2 y P-10), y el CI comprueba que el backend resuelve `data/` en la
> raíz de la carpeta y no dentro de `runtime/`.

## 18. Configuración

- Clave o mecanismo de autenticación de la API.
- Modelo de voz.
- Voz de salida.
- Micrófono de entrada.
- Dispositivo de salida.
- Frase de activación.
- Sensibilidad del detector.
- Duración máxima de respuesta.
- Carpeta de materiales.
- Política de conservación de transcripciones.
- Límites de consumo.

## 19. Tratamiento de errores

- Micrófono no disponible.
- Altavoces no disponibles.
- Permisos de audio bloqueados.
- Pérdida de conexión.
- API no disponible.
- Clave inválida o caducada.
- Modelo no autorizado.
- Documento ilegible.
- Fallo de extracción de texto.
- Consumo máximo alcanzado.
- Respuesta demasiado larga.

Los errores **no deben provocar que la aplicación parezca estar escuchando
cuando no lo está**. El estado real debe mostrarse siempre de forma visible.

## 20. Plan de implementación

| Fase | Alcance |
|---|---|
| **Fase 0** — Prueba técnica | Ejecutable portable mínimo, audio de entrada/salida, conexión Realtime y validación sin administrador |
| **Fase 1** — MVP | Carga de materiales, sesión, detector de activación, pregunta-respuesta oral y registro básico |
| **Fase 2** — Contexto | RAG local, resumen de sesión, historial reciente y referencias a documentos |
| **Fase 3** — Presentaciones | Visor de diapositivas y selección manual de diapositiva |
| **Fase 4** — Integraciones | Investigación de PowerPoint, noticias y herramientas externas |
| **Fase 5** — Optimización | Costes, latencia, robustez en aula y empaquetado final |

> **Nota de implementación.** La Fase 0 se acordó con tres entregables:
> diagnóstico completo, palabra de activación —adelantada desde la Fase 1 para
> medir pronto los falsos positivos— y carpeta portable verificada. La conexión
> Realtime se comprueba pero no se conversa (D-03).

## 21. Criterios de aceptación del MVP

- [ ] Se ejecuta en Windows sin permisos de administrador.
- [ ] No requiere Python, Node.js, Docker ni .NET previamente instalados.
- [ ] Permite cargar PDF, PPTX, DOCX, TXT y Markdown.
- [ ] Permite iniciar y finalizar una sesión.
- [ ] Detecta «Oye Chat» en condiciones razonables.
- [ ] No envía continuamente la conversación a la API durante la escucha pasiva.
- [ ] Responde oralmente a una pregunta.
- [ ] La respuesta utiliza los materiales cargados cuando son relevantes.
- [ ] El usuario puede interrumpir la respuesta.
- [ ] La aplicación muestra claramente sus estados.
- [ ] Los errores de red y audio se notifican.
- [ ] Se puede copiar y ejecutar desde una carpeta portable.

## 22. Pruebas necesarias

- Prueba en un PC del instituto sin privilegios administrativos.
- Prueba con micrófono integrado y micrófono USB.
- Prueba con altavoces y auriculares.
- Prueba en aula silenciosa y con ruido de fondo.
- Prueba con varias voces y acentos.
- Prueba de falsos positivos de «Oye Chat».
- Prueba de interrupción durante la respuesta.
- Prueba sin conexión y recuperación posterior.
- Prueba con documentos grandes.
- Prueba de borrado de sesiones y datos.
- Prueba de consumo durante una clase completa.

## 23. Decisiones explícitas

- La solución será una aplicación de escritorio Windows, no una aplicación web.
- La distribución principal será portable.
- Tauri 2 será la primera tecnología candidata para el contenedor de escritorio.
- React y TypeScript se utilizarán para la interfaz.
- Python se utilizará para procesamiento documental y contexto.
- El detector de activación se ejecutará localmente siempre que sea viable.
- La detección automática de PowerPoint queda fuera del MVP.
- No se utilizarán agentes autónomos complejos en la primera versión.
- La aplicación no hablará espontáneamente en el MVP.

## 24. Instrucciones para Claude Code o Codex

- Implementar por fases pequeñas y funcionales.
- Validar primero el audio y la ejecución portable antes de construir toda la
  arquitectura.
- Crear tests unitarios para extracción documental, fragmentación, recuperación
  y máquina de estados.
- Crear una prueba de humo para iniciar una sesión y realizar una pregunta.
- Mantener secretos fuera del repositorio.
- Documentar cómo construir la versión portable.
- No introducir dependencias que requieran instalación administrativa en el
  equipo final.
- Registrar decisiones técnicas y problemas encontrados.
- No añadir funciones no solicitadas al MVP.

## 25. Resultado esperado

El resultado será una aplicación portable que el profesor pueda abrir en un PC
del instituto, cargar los materiales de su módulo, pulsar «Iniciar clase» y
disponer de un asistente oral que permanezca silencioso hasta escuchar «Oye
Chat».

La solución deberá priorizar **la fiabilidad en el aula, la baja fricción de
uso, la privacidad, el control del coste y la compatibilidad con equipos sin
permisos de administrador**.
