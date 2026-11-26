# Carga histórica de estados financieros NIIF del SIMEV, de corrido.
#
# Cada corrida de `invest ingest sfc_rnve_niif` revisa hasta 80 cortes (unos 60 a 90 minutos) y termina;
# si el SIMEV falla 8 veces seguidas, se detiene antes. Este script las encadena hasta que no queden
# cortes pendientes:
#
#   - impide que Windows suspenda el PC mientras corre (una suspensión congela el navegador y la corrida);
#   - después de una corrida detenida por fallas del SIMEV espera 10 minutos y sigue;
#   - para si tres corridas seguidas no avanzan o si llega a 30 corridas;
#   - guarda (commit) los datos después de cada corrida y, al final, reconstruye las tablas
#     (invest dims, factores, ficha y modelo) y sube todo a GitHub.
#
# Uso:  powershell -ExecutionPolicy Bypass -File scripts\windows\carga_niif.ps1
#       (opcional: -MaxCorridas 5 -EsperaMinutos 10 -SinReconstruir)
#
# Se puede cerrar la ventana en cualquier momento: lo revisado queda guardado y la próxima vez sigue
# donde iba.

param(
    [int]$MaxCorridas = 30,
    [int]$EsperaMinutos = 10,
    [switch]$SinReconstruir
)

$ErrorActionPreference = "Continue"
$Pipeline = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Datos = Resolve-Path (Join-Path $Pipeline "..\invest-data")
$Log = Join-Path $Datos "meta\ejecucion_pc.log"
$env:INVEST_DATA_DIR = $Datos
$env:PYTHONIOENCODING = "utf-8"

function Registrar($texto) {
    $linea = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $texto
    Add-Content -Path $Log -Value $linea -Encoding utf8
    Write-Host $linea
}

function GuardarCambios($mensaje) {
    git -C $Datos add -A
    git -C $Datos diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git -C $Datos commit --quiet -m $mensaje
    }
}

# Mantener el PC despierto (la pantalla sí se puede apagar) mientras dure el script
Add-Type -Namespace Invest -Name Energia -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
"@
$ES_CONTINUOUS = [uint32]2147483648
$ES_SYSTEM_REQUIRED = [uint32]1
[Invest.Energia]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null

Push-Location $Pipeline
$pendientesAntes = $null
$sinAvance = 0
$detenidasSeguidas = 0
try {
    Registrar "Carga NIIF: inicio (hasta $MaxCorridas corridas)"
    for ($i = 1; $i -le $MaxCorridas; $i++) {
        Registrar "Carga NIIF: corrida $i"
        $salida = uv run invest ingest sfc_rnve_niif 2>&1 | ForEach-Object { Write-Host $_; "$_" }
        $texto = $salida -join "`n"
        GuardarCambios ("Carga NIIF corrida {0} {1:yyyy-MM-dd HH:mm}" -f $i, (Get-Date))

        if ($texto -match '\[sin_novedades\]') {
            Registrar "Carga NIIF: no quedan cortes pendientes"
            break
        }
        $m = [regex]::Match($texto, 'pendientes para otra corrida (\d+)')
        if (-not $m.Success) {
            Registrar "Carga NIIF: la corrida no terminó como se esperaba; se intenta otra vez en $EsperaMinutos minutos"
            $sinAvance++
            if ($sinAvance -ge 3) { Registrar "Carga NIIF: tres corridas sin avance, se detiene"; break }
            Start-Sleep -Seconds (60 * $EsperaMinutos)
            continue
        }
        $pendientes = [int]$m.Groups[1].Value
        Registrar "Carga NIIF: quedan $pendientes cortes pendientes"
        if ($pendientes -eq 0) { break }

        if ($null -ne $pendientesAntes -and $pendientes -ge $pendientesAntes) { $sinAvance++ } else { $sinAvance = 0 }
        $pendientesAntes = $pendientes
        if ($sinAvance -ge 3) { Registrar "Carga NIIF: tres corridas sin avance, se detiene"; break }

        if ($texto -match 'corrida detenida tras') {
            $detenidasSeguidas++
            if ($detenidasSeguidas -ge 3) {
                Registrar "Carga NIIF: el SIMEV falló en tres corridas seguidas; se detiene (pruebe más tarde)"
                break
            }
            Registrar "Carga NIIF: el SIMEV dejó de responder; se espera $EsperaMinutos minutos"
            Start-Sleep -Seconds (60 * $EsperaMinutos)
        } else {
            $detenidasSeguidas = 0
        }
    }

    if (-not $SinReconstruir) {
        Registrar "Carga NIIF: reconstruyendo tablas (invest dims, factores, ficha, modelo)"
        uv run invest dims 2>&1 | Out-Null
        # Los fundamentales nuevos cambian los factores de valor y utilidad: se revalidan y se rehace el puntaje
        uv run invest factores 2>&1 | Out-Null
        uv run invest ficha 2>&1 | Out-Null
        uv run invest modelo 2>&1 | Where-Object { "$_" -match '^\[contrato\]|^Contrato' } | ForEach-Object { Registrar $_ }
        GuardarCambios ("Carga NIIF: tablas reconstruidas {0:yyyy-MM-dd HH:mm}" -f (Get-Date))
    }
} finally {
    Pop-Location
    [Invest.Energia]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
    if (Test-Path (Join-Path $Datos ".git")) {
        $salidaGit = git -C $Datos pull --rebase --autostash --quiet 2>&1; $codigoGit = $LASTEXITCODE; $salidaGit | ForEach-Object { Registrar "git pull: $_" }
        $salidaGit = git -C $Datos push --quiet 2>&1; $codigoGit = $LASTEXITCODE; $salidaGit | ForEach-Object { Registrar "git push: $_" }
    }
    Registrar "Carga NIIF: fin"
}
