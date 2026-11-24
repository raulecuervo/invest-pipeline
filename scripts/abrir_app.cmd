@echo off
rem Aplicativo Invest: abre el navegador en http://127.0.0.1:8790/ (o el siguiente puerto libre). Cierre esta ventana para apagarlo.
cd /d "%~dp0.."
uv run --extra app --extra pc invest app
if errorlevel 1 pause
