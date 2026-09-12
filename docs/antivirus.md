# Antivirus y SmartScreen (riesgo R-7)

La versión portable es un ejecutable sin firmar que llega en una carpeta
copiada a mano. Eso es exactamente el perfil al que reaccionan SmartScreen y
los antivirus corporativos, así que conviene contarlo antes de que ocurra
delante de una clase.

## Por qué pasa

Tres cosas, ninguna de ellas un fallo de la aplicación:

1. **Sin firma digital.** Windows no puede decir quién publica el programa, así
   que SmartScreen avisa la primera vez que se ejecuta.
2. **Empaquetado con PyInstaller.** Un intérprete de Python dentro de un `.exe`
   que se descomprime en memoria es una técnica que también usa el malware, y
   varios motores heurísticos la marcan por sí sola. Es un falso positivo
   conocido de PyInstaller, no algo específico de este proyecto.
3. **Ejecución desde una carpeta de usuario o un USB**, que muchas políticas de
   centro vigilan con más celo que `Program Files`.

## Qué hacer, en orden de preferencia

### 1. Firmar el ejecutable

Es la solución de verdad. Si el centro dispone de un certificado de firma de
código:

```powershell
.\scripts\build-portable.ps1 `
    -CertificatePath C:\ruta\certificado.pfx `
    -CertificatePassword '...'
```

El script firma con SHA256 y sella el tiempo, de modo que la firma sigue
siendo válida cuando el certificado caduque. Con firma desaparece el aviso de
SmartScreen y baja mucho la probabilidad de que un antivirus intervenga.

### 2. Pedir una excepción al administrador

Sin certificado, lo razonable es que quien administra los equipos añada una
excepción para la carpeta de la aplicación. Hay que darle datos concretos:

- La ruta exacta desde la que se ejecuta.
- El archivo `SHA256SUMS.txt` que acompaña a la carpeta, para que pueda
  comprobar que no ha cambiado nada.
- Que el programa **no** necesita permisos de administrador, **no** escribe en
  el registro ni en `Program Files`, y **no** instala ningún servicio.

### 3. Verificar la carpeta antes de usarla

Cada construcción incluye `SHA256SUMS.txt`. Para comprobar que la copia es
íntegra:

```powershell
Get-Content SHA256SUMS.txt | ForEach-Object {
    $hash, $file = $_ -split '  ', 2
    $actual = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $hash) { Write-Host "DIFIERE: $file" -ForegroundColor Red }
}
```

Silencio significa que todo coincide.

## Si el antivirus ya ha borrado algo

Suele llevarse `runtime\backend\aiclassroom-backend.exe`, y entonces la
aplicación abre la ventana pero dice que no encuentra el motor local. No sirve
de nada volver a copiar la carpeta sin resolver antes la excepción: la
borrará otra vez.

## Antes de la primera clase

Prueba la carpeta en un equipo real del centro **con antelación**, no el mismo
día. Es la única forma de descubrir la política del instituto sin que te pille
con veinte alumnos delante. Anota el resultado en
`docs/decisiones-tecnicas.md`, junto al riesgo R-7.
