@echo off
rem Lectura manual de la Bolsa de Colombia (cierre, maximo, minimo e indices del dia).
rem Doble clic despues de las 4:15 p. m. de un dia bursatil; se abre una ventana de Chromium.
cd /d "%~dp0.."
uv run invest ingest bvc_web bvc_indices
echo.
pause
