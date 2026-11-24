# Ejecución diaria en el PC (Programador de tareas de Windows).
#
# 1. Trae los datos que GitHub Actions guardó en invest-data.
# 2. Corre los conectores que deben ejecutarse desde el PC (sitios que bloquean IP de centros de datos).
# 3. Si llegó algo nuevo, reconstruye las tablas derivadas, los factores y el tablero; siempre, la salud de datos.
# 4. Sube los cambios de datos al repo privado.
#
# Uso manual:  powershell -ExecutionPolicy Bypass -File scripts\windows\ejecutar_pc.ps1

# "Continue": en Windows PowerShell 5.1, con "Stop", cualquier línea que uv o git escriban por la
# salida de errores (avisos incluidos) detendría el script
$ErrorActionPreference = "Continue"
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

function GuardarCambios($mensaje) {
    git -C $Datos add -A
    git -C $Datos diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git -C $Datos commit --quiet -m $mensaje
        Registrar "commit: $mensaje"
    }
}

$script:HuboPull = $false
if (Test-Path (Join-Path $Datos ".git")) {
    # Lo que quedó de corridas manuales (BVC, carga del SIMEV) se guarda antes de traer lo de GitHub
    GuardarCambios ("Cambios locales {0:yyyy-MM-dd HH:mm}" -f (Get-Date))
    $antes = git -C $Datos rev-parse HEAD
    git -C $Datos pull --rebase --quiet 2>&1 | ForEach-Object { Registrar "git pull: $_" }
    $script:HuboPull = ($antes -ne (git -C $Datos rev-parse HEAD))
}

Push-Location $Pipeline
try {
    $salida = uv run invest ingest --todas-pc 2>&1 | ForEach-Object { Registrar $_; $_ }
    # Solo se reconstruye si llegó algo nuevo (aquí o en lo que trajo GitHub Actions con el git pull).
    # dims rehace las tablas derivadas (retornos, liquidez, fundamentales) y escribe solo los años que
    # cambian; factores rehace la validación mensual. Toman unos pocos minutos.
    $nuevo = ($salida | Where-Object { "$_" -match '^\[ok\]' -and "$_" -notmatch ' 0 filas v' }) -or $script:HuboPull
    if ($nuevo) {
        uv run invest dims 2>&1 | Out-Null
        uv run invest factores 2>&1 | Out-Null
        # Puntaje de los factores validados y tablero local (invest-data\tableros\matriz_factores.html)
        uv run invest ficha 2>&1 | Out-Null
        # Tablas del aplicativo (técnico, riesgo, valoración, puntajes, calendario, carteras), base
        # modelo.duckdb y verificación del contrato; los problemas quedan en la bitácora
        uv run invest modelo 2>&1 | Where-Object { "$_" -match '^\[contrato\]|^Contrato' } | ForEach-Object { Registrar $_ }
        Registrar "Tablas derivadas reconstruidas"
    } else {
        Registrar "Sin datos nuevos: no se reconstruye"
    }
    uv run invest salud 2>&1 | ForEach-Object { Registrar $_ }
} finally {
    Pop-Location
}

if (Test-Path (Join-Path $Datos ".git")) {
    GuardarCambios ("Ingesta PC {0:yyyy-MM-dd}" -f (Get-Date))
    git -C $Datos push --quiet 2>&1 | ForEach-Object { Registrar "git push: $_" }
    if ($LASTEXITCODE -ne 0) {
        # GitHub Actions subió algo mientras tanto: se rebasa y se intenta de nuevo
        git -C $Datos pull --rebase --quiet 2>&1 | ForEach-Object { Registrar "git pull: $_" }
        git -C $Datos push --quiet 2>&1 | ForEach-Object { Registrar "git push: $_" }
    }
}
Registrar "Fin"
