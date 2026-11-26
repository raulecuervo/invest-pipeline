# Recupera de una vez lo que quedó pendiente o falló: días de la Bolsa que no se leyeron, estados
# financieros de la información relevante de la SFC, documentos de estados de compañías por leer, y
# luego la corrida completa del PC (conectores, tablas derivadas, salud y subida de datos).
#
# Se corre a mano desde PowerShell (la Bolsa abre una ventana visible; no se esquiva su protección):
#   powershell -ExecutionPolicy Bypass -File scripts\windows\recuperar.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\windows\recuperar.ps1 -Documentos 30
#
# -Documentos: cuántos documentos de estados de compañías se leen con la API en esta corrida (por
# omisión 6, como la tarea diaria). Cada uno cuesta de USD 0,15 a 1 (config/estados_compania.yaml).
#
# No correrlo entre las 7:00 y las 7:50 p. m.: a esa hora suben datos GitHub Actions (7:17) y la tarea
# diaria del PC (7:47), y los tres escribirían a la vez en invest-data.
param([int]$Documentos = 6)

$ErrorActionPreference = "Continue"
$Pipeline = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Datos = Resolve-Path (Join-Path $Pipeline "..\invest-data")
$env:INVEST_DATA_DIR = $Datos
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Paso($texto) { Write-Host ""; Write-Host "== $texto" -ForegroundColor Cyan }

Paso "1. Código y datos al día (git pull)"
git -C $Pipeline pull --ff-only
git -C $Datos add -A
git -C $Datos diff --cached --quiet
if ($LASTEXITCODE -ne 0) { git -C $Datos commit --quiet -m ("Cambios locales {0:yyyy-MM-dd HH:mm}" -f (Get-Date)) }
git -C $Datos pull --rebase --autostash --quiet

Push-Location $Pipeline
try {
    Paso "2. Bolsa de Colombia: días que faltan de las últimas 10 sesiones (se abre una ventana)"
    uv run invest ingest bvc_web bvc_indices

    Paso "3. Información relevante de la SFC: estados financieros que faltan (sin esperar a GitHub Actions)"
    uv run invest ingest sfc_informacion_relevante
    # Se sube de inmediato: es una fuente de GitHub Actions y así su próxima corrida parte de esto
    git -C $Datos add -A
    git -C $Datos diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git -C $Datos commit --quiet -m ("Información relevante desde el PC {0:yyyy-MM-dd HH:mm}" -f (Get-Date))
        git -C $Datos push --quiet
    }
} finally {
    Pop-Location
}

Paso "4. Corrida completa del PC (estados de compañías: hasta $Documentos documentos; tablas, salud y subida)"
$env:INVEST_EE_MAX_DOCUMENTOS = "$Documentos"
& (Join-Path $PSScriptRoot "ejecutar_pc.ps1")
Remove-Item Env:INVEST_EE_MAX_DOCUMENTOS -ErrorAction SilentlyContinue

Paso "Listo. Lo que siga pendiente queda en la bitácora (invest-data\meta\ejecucion_pc.log) y en Salud de datos."
