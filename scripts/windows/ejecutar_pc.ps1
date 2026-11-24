# Ejecución diaria en el PC (Programador de tareas de Windows).
#
# 1. Trae los datos que GitHub Actions guardó en invest-data.
# 2. Corre los conectores que deben ejecutarse desde el PC (sitios que bloquean IP de centros de datos).
# 3. Reconstruye dimensiones y salud de datos.
# 4. Sube los cambios de datos al repo privado.
#
# Uso manual:  powershell -ExecutionPolicy Bypass -File scripts\windows\ejecutar_pc.ps1

$ErrorActionPreference = "Stop"
$Pipeline = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Datos = Resolve-Path (Join-Path $Pipeline "..\invest-data")
$Log = Join-Path $Datos "meta\ejecucion_pc.log"
$env:INVEST_DATA_DIR = $Datos

function Registrar($texto) {
    $linea = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $texto
    Add-Content -Path $Log -Value $linea -Encoding utf8
    Write-Output $linea
}

New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
Registrar "Inicio"

if (Test-Path (Join-Path $Datos ".git")) {
    git -C $Datos pull --quiet 2>&1 | ForEach-Object { Registrar "git pull: $_" }
}

Push-Location $Pipeline
try {
    uv run invest ingest --todas-pc 2>&1 | ForEach-Object { Registrar $_ }
    uv run invest dims 2>&1 | Out-Null
    uv run invest salud 2>&1 | ForEach-Object { Registrar $_ }
} finally {
    Pop-Location
}

if (Test-Path (Join-Path $Datos ".git")) {
    git -C $Datos add -A
    git -C $Datos diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git -C $Datos commit --quiet -m ("Ingesta PC {0:yyyy-MM-dd}" -f (Get-Date))
        git -C $Datos push --quiet 2>&1 | ForEach-Object { Registrar "git push: $_" }
    }
}
Registrar "Fin"
