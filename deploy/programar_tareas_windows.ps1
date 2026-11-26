<#
.SYNOPSIS
  Programa en Windows las tareas de operacion del Agente PQRS: respaldo diario, monitoreo cada 15 minutos y retencion semanal.

.DESCRIPTION
  Por defecto SOLO MUESTRA los comandos que ejecutaria (simulacion). Agregue -Aplicar para crearlas de verdad.
  Use -Quitar (con -Aplicar) para borrarlas. Las tareas se crean para el usuario que ejecuta este script: para produccion
  ejecutelo con la cuenta de servicio que tiene acceso a la base y a las variables de entorno de la app.

  IMPORTANTE: las tareas necesitan las mismas variables de entorno que la app (DATABASE_URL, PQRS_RESPALDO_DIR, PQRS_SMTP_*, ...).
  Definalas como variables de entorno del usuario o de la maquina para esa cuenta; este script NO las escribe ni guarda secretos.

.EXAMPLE
  .\programar_tareas_windows.ps1 -Proyecto C:\pqrs -DirRespaldos D:\respaldos -ClaveRespaldo D:\claves\respaldo.key -CopiaExterna E:\externo -CorreoAlertas admin@tuempresa.com
  (simulacion: muestra los comandos)

.EXAMPLE
  .\programar_tareas_windows.ps1 -Proyecto C:\pqrs -DirRespaldos D:\respaldos -ClaveRespaldo D:\claves\respaldo.key -Aplicar
#>
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[^"&;|<>`$%^]+$')][string]$Proyecto,
    [Parameter(Mandatory = $true)][ValidatePattern('^[^"&;|<>`$%^]+$')][string]$DirRespaldos,
    [ValidatePattern('^[^"&;|<>`$%^]*$')][string]$ClaveRespaldo = "",
    [ValidatePattern('^[^"&;|<>`$%^]*$')][string]$CopiaExterna = "",
    [ValidatePattern('^[A-Za-z0-9._%+-]*@?[A-Za-z0-9.-]*$')][string]$CorreoAlertas = "",
    [ValidatePattern('^[^"&;|<>`$%^]+$')][string]$Python = "python",
    [switch]$Aplicar,
    [switch]$Quitar
)

$ErrorActionPreference = "Stop"
$tareas = @("PQRS respaldo", "PQRS monitoreo", "PQRS retencion")

if ($Quitar) {
    foreach ($t in $tareas) {
        $cmd = "schtasks /Delete /TN `"$t`" /F"
        if ($Aplicar) { Write-Host "Borrando: $t"; Invoke-Expression $cmd } else { Write-Host "[simulacion] $cmd" }
    }
    return
}

foreach ($ruta in @("herramientas\respaldo.py", "herramientas\monitoreo.py", "herramientas\retencion.py")) {
    if (-not (Test-Path (Join-Path $Proyecto $ruta))) { throw "No encuentro $ruta dentro de $Proyecto. Revise -Proyecto." }
}

$h = Join-Path $Proyecto "herramientas"
$respaldo = "$Python `"$h\respaldo.py`" crear --destino `"$DirRespaldos`" --conservar 14"
if ($ClaveRespaldo) { $respaldo += " --clave-archivo `"$ClaveRespaldo`"" }
if ($CopiaExterna)  { $respaldo += " --copiar-a `"$CopiaExterna`"" }
$monitoreo = "$Python `"$h\monitoreo.py`""
if ($CorreoAlertas) { $monitoreo += " --correo-a $CorreoAlertas" }
$retencion = "$Python `"$h\retencion.py`" antiguas --dias 730 --aplicar"

$plan = @(
    @{ Nombre = "PQRS respaldo";   Programacion = "/SC DAILY /ST 03:00";          Comando = $respaldo },
    @{ Nombre = "PQRS monitoreo";  Programacion = "/SC MINUTE /MO 15";            Comando = $monitoreo },
    @{ Nombre = "PQRS retencion";  Programacion = "/SC WEEKLY /D SUN /ST 04:00";  Comando = $retencion }
)

if (-not $ClaveRespaldo) { Write-Warning "Sin -ClaveRespaldo el respaldo queda SIN CIFRAR y contiene datos personales." }
if (-not $CopiaExterna)  { Write-Warning "Sin -CopiaExterna los respaldos quedan solo en este equipo: un fallo de disco los pierde junto con la base." }

foreach ($p in $plan) {
    # /TR lleva el comando entre comillas; las comillas internas se escapan con \"
    $tr = $p.Comando.Replace('"', '\"')
    $cmd = "schtasks /Create /F /TN `"$($p.Nombre)`" $($p.Programacion) /TR `"$tr`""
    if ($Aplicar) {
        Write-Host "Creando tarea: $($p.Nombre)"
        Invoke-Expression $cmd
    } else {
        Write-Host "[simulacion] $cmd"
    }
}
if (-not $Aplicar) { Write-Host "`nNo se creo nada. Agregue -Aplicar para crear las tareas." }
else { Write-Host "`nListo. Verifique con: schtasks /Query /TN `"PQRS respaldo`" /V /FO LIST" }
