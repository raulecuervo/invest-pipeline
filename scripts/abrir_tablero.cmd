@echo off
rem Tablero de la matriz de factores: recalcula el puntaje con los datos que ya hay y lo abre.
rem No descarga nada; los datos se actualizan con la tarea diaria.
cd /d "%~dp0.."
uv run invest ficha --abrir
if errorlevel 1 pause
