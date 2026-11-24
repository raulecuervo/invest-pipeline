# Registra la tarea diaria en el Programador de tareas de Windows (una sola vez).
# Corre ejecutar_pc.ps1 todos los días a las 19:47, después de la ingesta de GitHub Actions (19:17).
# Si el PC está apagado a esa hora, la tarea corre apenas se encienda (StartWhenAvailable).
# Límite de 3 horas: mientras dura la carga histórica del SIMEV, cada corrida lee hasta 80 informes (una hora).
#
# Uso:  powershell -ExecutionPolicy Bypass -File scripts\windows\registrar_tarea.ps1

$Script = Resolve-Path (Join-Path $PSScriptRoot "ejecutar_pc.ps1")
$Accion = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`""
$Disparador = New-ScheduledTaskTrigger -Daily -At "19:47"
$Opciones = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName "Invest - ingesta diaria PC" `
    -Action $Accion -Trigger $Disparador -Settings $Opciones `
    -Description "Sistema de análisis de inversiones: conectores locales, dimensiones y salud de datos" `
    -Force | Out-Null

Write-Output "Tarea 'Invest - ingesta diaria PC' registrada para las 19:47 todos los días."
Write-Output "Para probarla ahora: Start-ScheduledTask -TaskName 'Invest - ingesta diaria PC'"
