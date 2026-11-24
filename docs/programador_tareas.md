# Tareas en el PC (Programador de tareas de Windows)

Algunas fuentes bloquean las IP de centros de datos como las de GitHub (bvc.com.co, el portal del Banco de la República). Esos conectores se marcan con `ejecuta_en: pc` en `config/catalogo_fuentes.yaml` y corren desde el PC.

La Bolsa de Colombia, además, bloquea (403) los navegadores sin ventana. Sus conectores (`bvc_web`, `bvc_indices`) tienen `ejecuta_en: manual`: no van en la tarea programada; se lanzan a mano después de las 4:15 p. m. con `uv run invest ingest bvc_web bvc_indices`.

## Registrar la tarea

```powershell
cd Proyectos\invest-pipeline
powershell -ExecutionPolicy Bypass -File scripts\windows\registrar_tarea.ps1
```

Queda la tarea **Invest - ingesta diaria PC**, todos los días a las 19:47. Si el PC está apagado, corre apenas se encienda.

## Qué hace

`scripts\windows\ejecutar_pc.ps1`:

1. `git pull` en `invest-data` (trae lo que guardó GitHub Actions).
2. `invest ingest --todas-pc`.
3. `invest dims` y `invest salud`.
4. Commit y push de los cambios en `invest-data`.

La bitácora queda en `invest-data\meta\ejecucion_pc.log`.

En la fase 0 todavía no hay conectores de PC; la tarea ya sirve para mantener las dimensiones y la salud de datos al día. Si GitHub Actions no estuviera disponible, se puede cambiar `ejecuta_en` de la TRM a `pc` y todo corre desde aquí.
