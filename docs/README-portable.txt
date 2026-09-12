AI CLASSROOM LIVE - version portable (Fase 0)
=============================================

Esta carpeta no necesita instalacion ni permisos de administrador. Copiala
donde quieras -- disco, memoria USB o unidad de red -- y ejecuta:

    AI-Classroom-Live.exe


QUE HACE ESTA VERSION
---------------------

Esta es la Fase 0, la prueba tecnica. Sirve para comprobar que la aplicacion
funciona en los equipos del instituto antes de construir el resto. Incluye:

  - Diagnostico del equipo: microfono, altavoces, DNS, certificados, clave de
    la API y conexion con el servicio de voz.
  - Deteccion local de la palabra de activacion "Oye Chat", con un medidor en
    pantalla para ajustar la sensibilidad.
  - Los estados de la sesion siempre visibles, incluido si el microfono esta
    abierto o cerrado.

TODAVIA NO responde a preguntas: la conversacion es trabajo de la Fase 1.


PRIMERA VEZ
-----------

1. Abre AI-Classroom-Live.exe.
2. En Configuracion, introduce la clave de la API y elige microfono y altavoces.
3. Pulsa "Comprobar equipo" y resuelve lo que aparezca en rojo.
4. Pulsa "Preparar sesion" y despues "Iniciar clase".
5. Di "Oye Chat" y comprueba que el estado cambia a "Activado".


LA CLAVE DE LA API
------------------

Hay dos formas de guardarla, y la eliges en Configuracion:

  Ligada a este equipo (predeterminada)
      Se cifra con tu cuenta de Windows. Es lo mas comodo: no hay que
      escribir nada al empezar la clase. Si copias la carpeta a otro
      ordenador, tendras que volver a introducir la clave.

  Con contrasena
      Marca "Poder usar esta clave en otros ordenadores" y elige una
      contrasena. La clave viaja con la carpeta y funciona en cualquier
      equipo del centro, pero tendras que escribir la contrasena al
      empezar cada sesion. Si la olvidas, habra que introducir la clave
      de nuevo: no hay forma de recuperarla.

En los dos casos la clave se guarda cifrada en data\config\ y nunca se
muestra en pantalla.


TUS DATOS
---------

Todo lo que genera la aplicacion vive en la carpeta data\:

    data\config\      configuracion y clave cifrada
    data\materials\   materiales docentes (a partir de la Fase 1)
    data\sessions\    sesiones
    data\metrics\     metricas de uso
    data\models\      modelos de la palabra de activacion

Para borrar todo, borra la carpeta data\. Para llevarte la configuracion a
otro equipo, copia data\ y vuelve a introducir la clave.

Durante la escucha pasiva no sale audio del equipo: la palabra de activacion
se detecta en local. Solo se envia audio tras la activacion, y en esta Fase 0
ni siquiera eso.


SI ALGO FALLA
-------------

  La ventana dice "No se encontro el motor local"
      El antivirus del centro puede haber bloqueado runtime\backend\. Comprueba
      que la carpeta esta completa.

  El diagnostico falla en certificados o en la conexion
      La red del centro puede estar inspeccionando el trafico HTTPS o filtrando
      las conexiones WebSocket. Cada comprobacion indica que hacer.

  No detecta "Oye Chat"
      Sube la sensibilidad en Configuracion y observa el medidor mientras
      hablas. Si se activa sola, bajala.

  Se activa solo, sin que nadie diga la frase
      Comprueba que "Activar solo cuando haya voz" esta marcado: descarta
      todo lo que no sea una persona hablando. Si aun asi ocurre, baja la
      sensibilidad.

  Windows avisa de que el programa no es de confianza
      Es normal: el ejecutable no va firmado. Ver docs\antivirus.md, que
      explica que pedir al administrador del centro.

  Falta el modelo de activacion
      En data\models\ deben estar:
          oye_chat.onnx
          openwakeword\melspectrogram.onnx
          openwakeword\embedding_model.onnx
      Los descarga y genera quien construye la version portable; el equipo
      del aula no descarga nada.
