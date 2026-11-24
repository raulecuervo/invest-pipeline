# Configurar GitHub (una sola vez)

> **Estado (23-nov-2026):** los pasos 1 a 3 ya están hechos. `raulecuervo/invest-pipeline` (público) e `invest-data` (privado) existen, tienen el contenido y las carpetas del PC están enlazadas a ellos. Los workflows ya están en `.github/workflows`, pero la ingesta y el mantenimiento no corren hasta que exista la variable `DATA_REPO` (paso 5). Faltan los pasos 4 y 5, que solo puede hacer el dueño de la cuenta porque el token no debe pasar por el chat. Para subir cambios desde el PC hace falta Git para Windows (`winget install --id Git.Git -e`); la primera vez que haga `git push` se abre el navegador para iniciar sesión.

El código va en un repo **público** (minutos de GitHub Actions ilimitados y cron que sí corre) y los datos en un repo **privado**. El workflow corre en el público y escribe en el privado con un token.

## 1. Crear los repositorios

En GitHub, crear:

- `invest-pipeline` — **público**, vacío (sin README).
- `invest-data` — **privado**, vacío.

## 2. Activar los workflows

Los workflows vienen en `docs\workflows\` porque la carpeta `.github` no se puede escribir de forma remota. Revísalos y cópialos a su lugar:

```powershell
cd Proyectos\invest-pipeline
New-Item -ItemType Directory -Force .github\workflows | Out-Null
Copy-Item docs\workflows\*.yml .github\workflows\
```

## 3. Subir el contenido local

Si no tiene Git, instálelo primero (una vez) y abra una PowerShell nueva:

```powershell
winget install --id Git.Git -e
```

Desde PowerShell en `Proyectos`:

```powershell
cd invest-pipeline
git init -b main
git add -A
git commit -m "Fase 0: cimientos y conector TRM"
git remote add origin https://github.com/<usuario>/invest-pipeline.git
git push -u origin main

cd ..\invest-data
git init -b main
git add -A
git commit -m "Estructura inicial de datos"
git remote add origin https://github.com/<usuario>/invest-data.git
git push -u origin main
```

## 4. Token para que el workflow escriba en `invest-data`

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.
2. Repository access: **Only select repositories** → `invest-data`.
3. Permissions → Repository permissions → **Contents: Read and write**. Nada más.
4. Expiración: un año, con recordatorio para renovarlo.

## 5. Secretos y variables en `invest-pipeline`

En `invest-pipeline` → Settings → Secrets and variables → Actions:

| Tipo | Nombre | Valor |
| --- | --- | --- |
| Secret | `DATA_REPO_TOKEN` | El token del paso 4 |
| Secret (opcional) | `SOCRATA_APP_TOKEN` | App token gratuito de datos.gov.co |
| Secret | `SEC_USER_AGENT` | Su nombre y un correo de contacto, p. ej. `Nombre Apellido correo@dominio.com`. La SEC lo exige para consultar EDGAR; sin él, ese conector falla y los demás siguen |
| Secret (opcional) | `ANTHROPIC_API_KEY` | Clave de la API de Claude ([console.anthropic.com](https://console.anthropic.com)), para leer los PDF de dividendos con dos agentes. Es de pago por uso y aparte de la suscripción de claude.ai. Sin ella, ese paso se omite |
| Variable | `DATA_REPO` | `<usuario>/invest-data` |

Los secretos no aparecen en los registros, aunque el repo sea público. Los workflows que vienen de *forks* no reciben secretos.

## 6. Probar

1. `invest-pipeline` → Actions → **Ingesta diaria** → Run workflow. La primera vez trae todo el histórico de la TRM.
2. Revisar que en `invest-data` aparezca un commit con `silver/fact_fx_diaria/`.
3. Desde entonces corre solo, todos los días a las 19:17 hora de Bogotá.

## Cuidados

- **Registros públicos:** cualquiera puede ver los logs de Actions. El código imprime solo conteos y estados; no agregar `print` de datos.
- **Inactividad:** el workflow *Mantenimiento mensual* hace un commit el día 1 de cada mes para que GitHub no apague la programación tras 60 días.
- **Dividendos:** el paso `invest dividendos` deja `revision/dividendos.csv` en `invest-data`. Para revisarlo: abrirlo en Excel, escribir `aprobado`, `corregido` (y corregir los valores) o `rechazado` en la columna `revision`, guardar como CSV y subirlo al repo. Lo marcado no se vuelve a sobrescribir.
- **Fallos:** si un paso falla, GitHub envía un correo. La página "Salud de datos" del tablero muestra el estado de cada fuente.
