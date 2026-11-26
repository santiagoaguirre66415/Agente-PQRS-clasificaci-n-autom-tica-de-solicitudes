<#
.SYNOPSIS
  Restringe en Windows quien puede leer los archivos sensibles del Agente PQRS (base de datos, claves, secretos).

.DESCRIPTION
  Por defecto SOLO MUESTRA los comandos icacls que ejecutaria (simulacion). Agregue -Aplicar para ejecutarlos.
  Quita la herencia de permisos y deja acceso completo solo a: la cuenta indicada (-Cuenta), SYSTEM y Administradores.
  Recomendacion: -Cuenta debe ser una cuenta de servicio dedicada, no un usuario con sesion interactiva.

  CUIDADO: si -Cuenta no es la cuenta con la que corre la app, la app dejara de poder leer sus archivos. Pruebe primero con un archivo de prueba.
  Use -Mostrar para listar los permisos actuales sin cambiar nada.

.EXAMPLE
  .\restringir_permisos_windows.ps1 -Ruta C:\pqrs\pqrs.db, D:\claves\respaldo.key, D:\claves\smtp.clave -Cuenta "MIEMPRESA\pqrs-servicio"
  (simulacion)

.EXAMPLE
  .\restringir_permisos_windows.ps1 -Ruta C:\pqrs\pqrs.db -Cuenta "MIEMPRESA\pqrs-servicio" -Aplicar
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Ruta,
    [string]$Cuenta = "",
    [switch]$Aplicar,
    [switch]$Mostrar
)

$ErrorActionPreference = "Stop"

foreach ($r in $Ruta) {
    if (-not (Test-Path -LiteralPath $r)) { throw "No existe: $r" }
}

if ($Mostrar) {
    foreach ($r in $Ruta) { Write-Host "=== $r ==="; icacls $r }
    return
}

if (-not $Cuenta) { throw "Indique -Cuenta (la cuenta con la que corre la app)." }
if ($Cuenta -match '[";&|<>`$%^]') { throw "-Cuenta contiene caracteres no permitidos." }

foreach ($r in $Ruta) {
    $args = @($r, "/inheritance:r", "/grant:r", "${Cuenta}:(F)", "/grant:r", "SYSTEM:(F)", "/grant:r", "*S-1-5-32-544:(F)")
    if ($Aplicar) {
        Write-Host "Restringiendo: $r"
        & icacls @args | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "icacls fallo sobre $r" }
    } else {
        Write-Host "[simulacion] icacls `"$r`" /inheritance:r /grant:r `"${Cuenta}:(F)`" /grant:r `"SYSTEM:(F)`" /grant:r `"*S-1-5-32-544:(F)`""
    }
}
if (-not $Aplicar) { Write-Host "`nNo se cambio nada. Agregue -Aplicar para ejecutarlo. (*S-1-5-32-544 es el grupo Administradores, en cualquier idioma.)" }
