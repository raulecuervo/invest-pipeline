# invest-pipeline

Código del sistema personal de análisis de activos financieros: Colombia (Bolsa de Colombia) primero, luego Chile, Perú, EE. UU. y otros países. El diseño completo está en el documento "Sistema de análisis de activos financieros — Colombia (BVC) y escalamiento por país".

Este repositorio es **público** y solo contiene código, configuración y documentación. Los datos viven en el repositorio **privado** `invest-data`, que va en una carpeta hermana:

```
Proyectos/
├── invest-pipeline/   ← este repo (público)
└── invest-data/       ← datos (privado): bronze, silver, gold, cuarentena, meta
```

## Estado

| Fase | Contenido | Estado |
| --- | --- | --- |
| 0 · Cimientos | Estructura, catálogo de fuentes, dimensiones (fecha, sesiones XBOG, país, moneda, clase de activo, índices, fuentes), almacenamiento por capas, bitácora y salud de datos, workflows | Hecho |
| 0 · Conector de prueba | TRM desde datos.gov.co, de punta a punta | Hecho |
| 1 · Precios de acciones (SFC) | Índice de especies, precio promedio y valor transado diarios desde 2007, ficha diaria con sector, Q de Tobin, liquidez e IBA; `dim_activo` | Hecho |
| 1 · Eventos corporativos (SFC) | Información relevante desde 2007 (dividendos, asambleas, OPA, readquisiciones, fusiones, suspensiones), clasificada y enlazada al emisor; PDF de dividendos recientes | Hecho |
| 1 · Dividendos desde los PDF | Dos agentes leen cada PDF, comparan, se revisan entre sí hasta un consenso; archivo de revisión editable en Excel; `fact_dividendo` | Hecho (requiere `ANTHROPIC_API_KEY`) |
| 1 · Dividendos desde historiales públicos | Investing.com (historial completo) y stockanalysis.com (últimos 20 pagos) para 19 acciones líquidas desde 2007, verificados con la regla ex-dividendo de la BVC, el precio y los avisos de la SFC; `revision/dividendos_web.csv` y `fact_dividendo_web` | Hecho (se usa mientras no haya dividendo de los PDF) |
| 1 · Retorno total | Índice de retorno total y precio ajustado por dividendos, splits y escisiones, solo con días de negociación real (`gold/fact_retorno_diario`) | Hecho |
| 1 · Escisiones | Lista curada con la relación oficial y su fuente (`config/eventos_capital.yaml`): BAC Holding 2022, Cementos Argos 2012 y 2025, Nutresa 2023, Sura y Argos 2025; `gold/fact_distribucion_especie` | Hecho |
| 1 · Liquidez | Monto medio y fracción de ruedas con negocios en 20 y 60 días, marca de acción líquida (`gold/fact_liquidez_diaria`) | Hecho |
| 1 · Macro y mercado | IPC del DANE (desde 2003) y FRED (Tesoro de EE. UU., inflación implícita, tasa larga de Colombia, VIX, diferencial emergente) en `silver/fact_serie_macro` (una carpeta por fuente) | Hecho |
| 1 · Fundamentales | CUIF mensual de 8 bancos (`silver/fact_cuif_mensual`) y XBRL de la SEC de Bancolombia/Cibest, Ecopetrol y Aval (`silver/fact_xbrl_sec`) | Hecho (SEC requiere `SEC_USER_AGENT`) |
| 1 · Estados de la compañía (PDF) | Respaldo consolidado de los emisores sin XBRL: PDF de los sitios de inversionistas leídos con dos agentes y consenso (`silver/fact_ee_compania`), con controles y cotejo contra el XBRL (`gold/fact_cotejo_pdf_compania`) | Hecho en código (24-nov-2026; requiere `ANTHROPIC_API_KEY`; Cementos Argos, Celsia y Terpel con PDF a mano) |
| 1 · Bolsa de Colombia y BanRep | Cierre, máximo y mínimo diarios de la BVC (`fact_precio_bvc`), MSCI COLCAP y MSCI NUAM USD, tasa de política monetaria e IBR en `fact_serie_macro`; con Playwright en el PC | Hecho (BanRep automático; BVC de lectura manual, primera lectura 23-nov-2026) |
| 1 · Factores y validación | Factores mensuales con validación estadística; dos validados (momentum 12-1 y rendimiento por dividendo) | Hecho |
| 1 · Matriz de factores | Puntaje por acción con los factores validados, ficha mensual y tablero local en HTML (`invest ficha`) | Hecho (24-nov-2026) |
| 1 · Modelo de datos del aplicativo | Emisores, precio diario con capitalización, macro, técnico, riesgo, métricas fundamentales, valoración, puntajes de 1 a 10, matriz, screeners, calendario, perfiles, carteras y desempeño; base `modelo.duckdb` con contrato verificado (`invest modelo`) | Hecho (24-nov-2026) |
| 1 · Aplicativo local | Web local (FastAPI + HTML/JS propio, estilo B): Mercado y país, Screener, Matriz, Ficha, Mi cartera, Carteras y transacciones, Perfiles, Tesis, Salud de datos y Aprender (`invest app`, `scripts\abrir_app.cmd`) | Hecho (24-nov-2026) |
| 1 · Plantilla de bancos | ROE, ROA y ROTE, margen de intereses, eficiencia, costo del riesgo, calidad y cobertura de la cartera con el CUIF mensual (`gold/fact_metrica_banco`); calidad de los bancos en el puntaje y sección Banco en la Ficha | Hecho (24-nov-2026; las cuentas nuevas llegan con la próxima corrida del CUIF) |
| 1 · Resto del MVP | NIIF histórico de la SFC (carga en curso), calculadora de rebalanceo, alertas | Pendiente |
| Opcional, al final | Modelo de Power BI sobre los mismos Parquet | Opcional |

## Puesta en marcha (Windows)

1. Instalar [uv](https://docs.astral.sh/uv/) si no está: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
2. En `Proyectos\invest-pipeline`:

```powershell
uv sync --extra dev          # crea el entorno e instala dependencias
uv run pytest                # pruebas (deben pasar todas)
uv run invest dims           # construye las dimensiones en ..\invest-data\gold\dims
uv run invest ingest trm_datos_gov   # primera vez: trae todo el histórico desde 1991
uv run invest salud          # muestra el último dato por fuente y si está al día

uv run invest inspeccionar   # prueba en vivo la estructura de la SFC con Ecopetrol, sin guardar nada
uv run invest ingest sfc_especies sfc_precios_promedio sfc_ficha_especie
                             # primera vez: unos 350 archivos y 6 a 8 minutos (1 s de pausa entre pedidos)
uv run invest ingest sfc_informacion_relevante
                             # primera vez: unos 240 meses desde 2007, de 10 a 15 minutos
uv run invest dims           # vuelve a correr para crear dim_activo

$env:SEC_USER_AGENT = "Nombre Apellido correo@dominio.com"   # la SEC exige identificarse
uv run invest ingest dane_ipc fred cuif_datos_gov sec_edgar
                             # primera vez: el CUIF baja 10 años de 8 bancos (varios minutos)

uv sync --extra dev --extra pc         # agrega Playwright (navegador real) para BVC y BanRep
uv run playwright install chromium     # una sola vez
uv run invest ingest banrep_tasas
uv run invest ingest bvc_web bvc_indices   # a mano, después de las 4:15 p. m. de un día hábil (abre una ventana)
uv run invest dims
```

La carpeta de datos se puede cambiar con la variable de entorno `INVEST_DATA_DIR`.

Opcional: un *app token* gratuito de datos.gov.co en la variable `SOCRATA_APP_TOKEN` sube el límite de consultas.

## Comandos

| Comando | Qué hace |
| --- | --- |
| `invest dims` | Construye las dimensiones conformadas |
| `invest ingest <fuente_id>` | Ingesta incremental de una fuente (desde el último dato menos `recarga_dias`) |
| `invest ingest <fuente_id> --desde AAAA-MM-DD --hasta AAAA-MM-DD` | Recarga un rango |
| `invest ingest --todas-actions` | Todas las fuentes implementadas que corren en GitHub Actions |
| `invest ingest --todas-pc` | Todas las que corren en el PC (sitios que bloquean IP de centros de datos) |
| `invest salud` | Reporte de salud de datos; devuelve código 1 si algo no está al día |
| `invest dividendos` | Lee con dos agentes los PDF de dividendos nuevos y actualiza el archivo de revisión y `fact_dividendo` |
| `invest dividendos --revision` | Solo vuelve a leer `revision/dividendos.csv` (después de revisarlo) y reconstruye `fact_dividendo` |
| `invest inspeccionar [especie]` | Descarga índice, ficha y serie de una especie de la SFC y muestra lo que entiende, sin guardar |
| `invest factores` | Factores mensuales y su validación estadística |
| `invest modelo [--sin-calcular]` | Tablas del aplicativo, base `invest-data/modelo.duckdb` y verificación del contrato (código 1 si hay problemas). Solo recalcula los pasos con insumos nuevos y dice cuáles saltó |
| `invest modelo --todo` | Lo mismo, recalculando todos los pasos aunque sus insumos no hayan cambiado |
| `invest app [--puerto 8790] [--no-abrir]` | Aplicativo local en el navegador (doble clic: `scripts\abrir_app.cmd`) |
| `invest usuario --plantillas` / `--importar TABLA ARCHIVO.csv` | Tablas vacías y CSV de ejemplo para perfiles, carteras y transacciones; carga validada desde Excel |
| `invest ficha [--abrir]` | Puntaje de los factores validados por acción y tablero `invest-data/tableros/matriz_factores.html` (`scripts\abrir_tablero.cmd` lo hace con doble clic) |

## Solo lo nuevo en cada corrida

Después de la primera carga, ninguna corrida vuelve a bajar lo que ya tiene:

| Fuente | Cómo evita repetir |
| --- | --- |
| TRM | Pide a la API solo desde el último dato guardado menos 10 días (correcciones) |
| Índice SFC | Descarga condicional: si la página no cambió, el servidor responde "sin cambios" (304) sin contenido |
| Ficha y precios SFC | Si la SFC no publicó un reporte nuevo, no se pide nada. Si lo publicó: fichas con descarga condicional y el .xls de precios solo de las acciones cuya ficha muestra una operación posterior a la última guardada |
| Información relevante | Consulta solo desde la última fecha guardada menos 3 días; los PDF ya guardados no se vuelven a bajar |
| Dividendos (dos agentes) | Solo los PDF nuevos o que cambiaron; lo ya procesado no se vuelve a enviar |
| Estados de la compañía | Páginas índice con descarga condicional; un PDF ya guardado no se vuelve a pedir; un 404 de una plantilla se reintenta a los 7 días; solo se envía a la API un documento nuevo o que cambió |

La memoria de cada fuente (validadores HTTP y última fecha de reporte) está en `meta/estado_fuentes/` y solo se actualiza si la corrida termina sin error. `--desde` fuerza la descarga completa de ese rango (por ejemplo, una recarga mensual para captar correcciones de la SFC).

## Cómo funciona cada ejecución

1. **Descarga** de la fuente (con reintentos, pausas y User-Agent propio).
2. **Bronze:** la respuesta cruda se guarda tal cual en `bronze/<fuente>/fecha_ingesta=AAAA-MM-DD/` (o en `bronze/_efimero/` si la fuente tiene `bronze_en_repositorio: false`).
3. **Interpretación y validación:** filas tipadas; las anómalas van a `cuarentena/` con su motivo.
4. **Silver:** las filas válidas se insertan por clave en `silver/<tabla>/anio=AAAA/parte.parquet`; solo se reescriben los años que cambian.
5. **Bitácora:** cada ejecución queda en `meta/ejecuciones.jsonl`; `invest salud` la usa.

## Convenciones

Están en `config/convenciones.yaml`. La más importante por ahora: la TRM vigente en el día *t* se calcula con las operaciones del día hábil anterior. Por eso `fact_fx_diaria` tiene `fecha` (día de vigencia) y `fecha_mercado` (día de las operaciones). Para convertir un cierre del día *t* se usa la fila con `fecha_mercado = t`.

## Acciones colombianas: fuente SFC

La Superintendencia Financiera publica, sin registro, tres piezas por especie. El conector las lee así:

| Fuente (`catalogo_fuentes.yaml`) | Página | Tabla silver | Clave |
| --- | --- | --- | --- |
| `sfc_especies` | `ppal_promedios.htm` (117 especies, incluidas las que ya no cotizan) | `dim_especie_sfc` | fecha_reporte, especie |
| `sfc_precios_promedio` | `<especie>_<año>.xls` y `<especie>_ant.xls` (Excel BIFF2, leído con `invest.biff2`) | `fact_precio_sfc` | fecha, especie |
| `sfc_ficha_especie` | `d_<especie>.htm` | `fact_indicadores_sfc` | fecha_reporte, especie |

- **Especie:** el código de la página en minúsculas. `coc04paao008` = emisor `coc04` (Ecopetrol), clase `ao` (ordinaria). Clases: `ao` ordinaria, `ad` preferencial, `ae` sin dividendo, `ap` privilegiada; `DERECHOS` en el nombre = derechos.
- **Rezago:** uno o dos días hábiles. Es la fuente oficial gratuita; la Bolsa de Colombia no ofrece API abierta (la suya exige un token de sesión que no se replica).
- **Datos que la fuente marca y se separan:** filas con precio 1 y valor 1 (días sin negociación, p. ej. antes de listarse) van a cuarentena como `marcador_sin_negociacion`, sin alarma; días repetidos con valores idénticos se dejan una vez; repetidos con valores distintos van a cuarentena y marcan `revisar`. En la ficha, valor patrimonial 1.00 = sin dato.
- **Corrida diaria:** solo baja el archivo del año en curso y omite especies sin negociación en 400 días. `--desde` en un año anterior vuelve a bajar `_ant.xls`.
- **Crudos:** se guardan como un .zip por ejecución en `bronze/_efimero/` (no se versiona; en GitHub Actions queda como artefacto 30 días). Solo se permite para fuentes `redistribuible: true`.
- Si la SFC bloqueara las IP de GitHub (errores repetidos en Actions), cambie `ejecuta_en: actions` por `pc` en las tres fuentes.

## Eventos corporativos: información relevante de la SFC

`sfc_informacion_relevante` usa la consulta pública "todas las entidades" del Registro Nacional de Valores y Emisores: un formulario JSF sin registro ni captcha. Pide mes a mes con tema "Todos" (en marzo, temporada de asambleas, llegan unos 1.200 registros) y la paginación de la tabla trae hasta 2.000 filas por pedido.

| Capa | Tabla | Contenido |
| --- | --- | --- |
| silver | `fact_evento_sfc` | fecha, hora, entidad, tema de la SFC, resumen completo, si tiene anexo y dónde quedó guardado. Clave `evento_id` |
| gold | `fact_evento_corporativo` | lo anterior + `categoria`, `afecta_precio` y `emisor_id`; lo arma `invest dims` |

- **Categorías** (`src/invest/eventos.py`, reglas sobre el tema y el resumen): `ex_dividendo`, `dividendo`, `opa`, `readquisicion`, `escision_fusion`, `capital`, `suspension`, `desliste`, `asamblea_convocatoria`, `asamblea_decision`, `resultados`, `calificacion`, `gobierno`, `emision_deuda`, `otros`.
- **Emisor:** se enlaza solo si el nombre de la entidad coincide exactamente con el de una especie del índice, ya normalizado. Lo que no coincida y sí tenga acciones se corrige en `config/emisores_alias.yaml`.
- **Anexos:** el monto por acción y las fechas ex-dividendo casi siempre vienen en el PDF, no en el resumen. Se bajan solo los de dividendos, cambios de capital, fusiones y OPA de emisores con acciones, de los últimos 400 días (máximo 60 por corrida), a `invest-data/anexos/sfc/<año>/`. Leer esos PDF para llenar una tabla de dividendos es el paso siguiente.

## Dividendos desde historiales públicos (`src/invest/dividendos_web.py`)

Mientras no se lean los PDF de la SFC, el retorno total usa los historiales de dividendos en efectivo de Investing.com y stockanalysis.com, copiados a mano desde el navegador en `invest-data/fuentes_web/dividendos/` (repo privado; no se redistribuyen). La lista de acciones y las exclusiones están en `config/dividendos_web.yaml`.

- **Ajuste por splits:** las dos páginas publican los valores en acciones de hoy; se llevan al valor de la época con los splits de `gold/fact_split` (p. ej. la BVC: 225 publicado = 0,45 por acción antes de la reagrupación 500:1).
- **Duplicados:** el mismo valor a 5 días o menos es un solo pago; manda la fecha que confirma stockanalysis o la que cuadra con la regla ex-dividendo.
- **Verificación independiente:** fecha ex frente a la fecha de pago con la regla de la BVC (10 días hábiles hasta 2012, 4 después); pago frente al precio de la SFC (más de 15 % o 8 veces la mediana de los pagos cercanos se revisa: así salen las escisiones y los dividendos en acciones); avisos de la SFC (informativo, con montos que coinciden). stockanalysis parece usar el mismo proveedor que Investing: sirve para descartar duplicados y repartos que no son en efectivo, no como segunda fuente independiente.
- **Revisión:** `invest-data/revision/dividendos_web.csv` (`aprobado`, `corregido` con `valor_revisado`, `rechazado`; se pueden agregar filas a mano con su decisión). Luego `invest dims`.
- **Carga del 23-nov-2026:** 2.220 pagos de 22 acciones (Terpel, Grupo Bolívar y Banco de Occidente se sumaron ese día); 2.180 verificados solos; 28 decididos con fuentes (notas en el CSV): se rechazan las escisiones (se ajustan aparte, ver abajo); los dividendos de Aval y Banco de Bogotá de 2022 y el extraordinario de Grupo Bolívar de 2024 se aprueban como efectivo, porque las acciones solo se entregaban a quien las pedía; el de 593 de Ecopetrol en 2023 se carga en sus tres cuotas. 15 quedan fuera por las exclusiones configuradas (GEB antes de 2012, Promigas antes de oct-2014, Banco de Bogotá dic-2010 a ago-2011). Pendientes: las poco líquidas.
- **Corficolombiana y Conconcreto (24-nov-2026):** Corficolombiana pagó en acciones nuevas de 2017 a 2022 y las dos páginas lo muestran como efectivo, así que ese periodo se excluye; desde las utilidades de 2023 la ordinaria no recibe dividendo y la preferencial solo el mínimo (1.135, 1.194 y 1.255 en 2024–2026). Quedan 15 pagos de la ordinaria (cuotas mensuales de may-2016 a mar-2017 y las tres de 2023) y 6 de la preferencial (solo stockanalysis: la página de Investing no carga). Antes de may-2016 falta (Investing solo muestra 20 filas): sale de los avisos de la SFC. Conconcreto está en Investing como `conconcret`; no decretó dividendo de los ejercicios 2017–2025 salvo uno no gravado de 15.000 millones con utilidades anteriores a 2016 (3 cuotas de 4,40818 en 2022–2023). Hasta abr-2013 Investing ajusta sus valores por 0,926196 y mezcla un dividendo en acciones: se excluye y la cuota de abr-2013 queda corregida a 10 en la revisión. Respaldo oficial de Conconcreto: la tabla de dividendos de `conconcreto.com/detalle-de-la-accion/` y el PDF anual "IR decisiones asamblea".
- **Mineros (pagos en dólares):** Investing mezcla filas en USD con filas ya convertidas a pesos. En el archivo, cada fila en dólares lleva un quinto campo `USD`; el rendimiento se mide en pesos con la TRM del día ex y el retorno total convierte con esa misma TRM. 136 pagos desde 2007, todos verificados; el extraordinario de USD 0,20 (nov-2026) coincide con el aviso de la SFC.

## Dividendos: dos agentes, consenso y revisión

`invest dividendos` toma los PDF de dividendos guardados por `sfc_informacion_relevante` y los lee con dos agentes independientes (API de Claude; modelos e instrucciones en `config/extraccion.yaml`):

1. **Lectura independiente.** El agente A (analista) y el agente B (auditor) leen el PDF original con modelos e instrucciones distintos. Cada uno devuelve tipo de documento (proyecto, aprobado, fechas ex-dividendo...), ejercicio y, por clase de acción, el valor por acción y sus cuotas con fecha ex-dividendo y de pago, más una cita literal.
2. **Verificación.** Cada cita se busca en el texto del PDF; si no aparece, cuenta como diferencia.
3. **Revisión cruzada.** Si difieren, cada agente ve su respuesta, la del otro y las diferencias, vuelve a leer el PDF y corrige o sostiene su lectura (hasta 2 rondas).
4. **Resultado.** `consenso`, `sin_dividendo` o `en_disputa`.

Todo queda en `invest-data/revision/dividendos.csv` (punto y coma, abre en Excel), una fila por cuota con las dos propuestas y las diferencias. En la columna `revision` se escribe `aprobado`, `corregido` (corrigiendo los valores; coma decimal, fechas AAAA-MM-DD o dd/mm/aaaa) o `rechazado`; luego `invest dividendos --revision`. Lo marcado no se sobrescribe.

`gold/fact_dividendo` usa lo corregido, lo aprobado y lo que tuvo consenso; lo que está en disputa sin revisar no entra. Si varios documentos hablan del mismo dividendo, manda la corrección manual, luego la aprobación, luego el documento más reciente, y `conflicto_entre_documentos` marca cuando no coinciden. `solo_propuesto` avisa si solo hay un proyecto sin aprobación.

Sin `ANTHROPIC_API_KEY` el paso se omite y los PDF quedan pendientes. Los registros solo muestran conteos.

## Retorno total y precio ajustado

`invest dims` arma `gold/fact_retorno_diario` (`src/invest/retornos.py`): por acción y día negociado, el precio promedio ponderado de la SFC, el dividendo en efectivo que empieza su periodo ex ese día, lo entregado en especie (escisiones), el retorno de precio, el retorno total y un índice de retorno total (base 100).

- **Solo días con negociación real:** la SFC repite el último precio los días sin negocios (valor transado 0; son 226 mil de las 367 mil filas). Esas filas no entran: contarlas como retorno cero subestimaría la volatilidad de las poco líquidas. `dias_desde_anterior` dice cuántos días cubre cada retorno.

- **Precio ajustado** = índice × último precio / último índice (`retornos.precio_ajustado()` o una medida en Power BI). Sus retornos son exactamente los retornos totales: el dividendo se reinvierte al precio del primer día ex. Se guarda el índice y no el precio ajustado porque el índice no cambia hacia atrás cuando entra un dividendo nuevo.
- **Dividendos:** de `gold/fact_dividendo` (revisados y con consenso), en efectivo y aprobados; donde no hay, de `gold/fact_dividendo_web` (ver abajo). Un decreto "todas" aplica a ordinarias, preferenciales y privilegiadas del emisor. En dólares se convierte con la TRM del día ex.
- **Fecha ex-dividendo:** la del PDF; si no viene, se estima desde la fecha de pago con la regla de la época (`config/convenciones.yaml`: 10 días hábiles antes de 2012, 4 desde entonces) y queda marcada `fecha_ex_estimada`.
- **Splits y reagrupaciones** (`src/invest/splits.py`): la SFC no ajusta sus precios por esos eventos. Cada salto de más de 50 % en un día se clasifica: `error_puntual` (el precio vuelve: error de la fuente), `split_con_evento` (razón cercana a un factor redondo, nivel sostenido y un evento de la SFC que habla de split, fraccionamiento, consolidación, valor nominal, fusión o escisión: se aplica solo), `split_sin_evento` y `sin_explicacion` (esperan revisión). Todo menos los errores queda en `invest-data/revision/splits.csv`, y en la columna `revision` va: `aprobado`, `corregido` (con `factor_revisado`, p. ej. `1:10` o `500:1`), `rechazado` (no fue split; sigue marcado como salto atípico) o `mercado` (no fue split pero el salto es real: OPA, quiebra, reanudación tras una suspensión; no se ajusta ni se marca como atípico y queda en `gold/fact_salto_mercado`). Luego `invest dims`. Factor = acciones nuevas por cada anterior.
  - Revisión con fuentes web del 23-nov-2026 (notas en el CSV): 9 splits aplicados (Nutresa 2004 4:1, Bolívar 2009, Celsia 2010 10:1, GEB 2011 100:1, Canacol 2012 10:1 y 2023 5:1, Promigas 2013 7:1, BVC 2018 500:1, Éxito 2022 3:1), 1 rechazado (InterBolsa 2007: precio de referencia sin negociación) y 5 saltos de mercado (OPA de Nutresa en 2022 y Capítulo 11 de Avianca en 2020-2021). Los 86 restantes se decidieron con la serie de negocios: 13 `mercado` (reestructuración de Pacific 2016, GEB antes de la venta de Endesa en 2009, negocios aislados de acciones poco líquidas) y 73 `atipico` (antes de 2007 sin avisos para confirmar, o más de 60 días sin negocios antes del salto: no se ajustan y siguen fuera de volatilidad y backtests). Valores de `revision`: `aprobado`, `corregido`, `rechazado`, `mercado`, `atipico`.
- **Saltos sin ajustar** quedan marcados en `salto_atipico` para que volatilidad, señales y backtests los excluyan.
- **Escisiones** (`src/invest/distribuciones.py`, `config/eventos_capital.yaml`): el accionista conserva su acción y recibe acciones de otra sociedad. Valor entregado = relación oficial × precio SFC de la acción recibida el primer día con negocios desde la fecha ex (o un precio de referencia documentado si no negoció en 10 días: BHI y Sociedad Portafolio). Se suma al retorno total como un dividendo en especie y el salto no se marca como atípico. Ej.: Grupo Argos ordinaria 28-jul-2025, precio −30 %, retorno total +14 % (prensa: +13,6 %).
- **Sin ajustar todavía:** dividendos en acciones propias obligatorios (no hay casos en las 22 acciones con historial; los de Aval, Bogotá y Bolívar eran opcionales y se tratan como efectivo).
- Las tablas derivadas grandes se guardan por año y solo se reescribe el año que cambia, para que el repositorio de datos no crezca con copias.

## Liquidez (`src/invest/liquidez.py`)

`gold/fact_liquidez_diaria`: por acción y día de la serie SFC completa (incluidos los días sin negocios), el monto medio transado y la fracción de ruedas con negocios en 20 y 60 ruedas, y `liquida` si en 20 ruedas el promedio supera COP 200 millones y negoció al menos 80 % de los días (`config/convenciones.yaml`). El 18-nov-2026 cumplían 19 de 89 acciones. Las demás no se excluyen: usan retornos semanales o mensuales y beta de Dimson.

## Macro, mercado y fundamentales

| Fuente | Conector | Tabla silver | Notas |
| --- | --- | --- | --- |
| `dane_ipc` | `ingest/dane_ipc.py` | `fact_serie_macro` (`ipc_co`) | Anexo mensual del DANE (sin API): la página enlaza `anex-IPC-Indices-<mes><año>.xlsx`; serie de empalme desde 2003, base dic-2018 = 100. `fecha_publicacion` real para el último mes y estimada (5.º día hábil) para los anteriores |
| `fred` | `ingest/fred.py` | `fact_serie_macro` (`fred_<id>`) | CSV público sin clave; series en el catálogo. La serie del IPC de Colombia en FRED se cortó en 2023. Corre en el PC: desde GitHub Actions FRED no responde (24-nov-2026) |
| `cuif_datos_gov` | `ingest/cuif.py` | `fact_cuif_mensual` | Primer nivel de cuentas y las `cuentas_extra` de la plantilla de bancos, moneda total, 8 bancos inscritos; control activo = pasivo + patrimonio. La API tarda unos 40 s por consulta |
| `sec_edgar` | `ingest/sec_edgar.py` | `fact_xbrl_sec` | `companyfacts` de la SEC (NIIF, en pesos): todos los conceptos `ifrs-full` con formulario, radicación y fecha de presentación (para evitar sesgo de anticipación). Requiere `SEC_USER_AGENT` = "Nombre correo@dominio" |
| `estados_compania` | `ingest/estados_compania.py` | `fact_ee_compania` | Estados consolidados de los PDF de cada compañía con dos agentes (ver abajo). Corre en el PC; requiere `ANTHROPIC_API_KEY` |
| `supersociedades_10000` | `ingest/supersociedades.py` | `fact_ee_anual_supersociedades` | "10.000 empresas más grandes del país" (datos.gov.co, `6cat-2gcs`): ingresos, utilidad, activos, pasivos y patrimonio **separados** de cada cierre anual desde 2021, solo para los NIT de `config/emisores_rnve.yaml`. Billones con dos decimales (± 5.000 millones); los bancos no están. Respaldo y control de los anexos (ver abajo) |
| `banrep_tasas` | `ingest/banrep.py` | `fact_serie_macro` (`banrep_<id>`) | Páginas públicas de SUAMECA leídas con Playwright: se guardan las respuestas que la propia página recibe (`consultaMenuXId` y `consultaInformacionSerie`). TPM e IBR en todos sus plazos |
| `bvc_indices` | `ingest/bvc.py` | `fact_serie_macro` (`msci_colcap`, `msci_nuam_usd`) | Valor de hoy y de la sesión anterior, leídos de la página renderizada de la BVC |
| `bvc_web` | `ingest/bvc.py` | `fact_precio_bvc` | "Mercado local en línea": cierre, máximo, mínimo, volumen. Solo guarda si son más de las 4:15 p. m., es día hábil, la página trae la fecha de hoy y no dice "abierto". Nemotécnicos → especie SFC en `config/nemotecnicos.yaml`. No hay histórico: se acumula desde la primera corrida |

**Lectura manual de la Bolsa de Colombia.** La BVC (su CDN, CloudFront) responde 403 "Request blocked" a los navegadores sin ventana. No se esquiva: `bvc_web` y `bvc_indices` tienen `ejecuta_en: manual`, no entran en `--todas-pc` ni en tareas programadas, y sin consola (una tarea programada) se niegan a abrir la página. Se lanzan a mano y abren una ventana normal de Chromium, como si se abriera la página. Los días sin lectura quedan sin cierre/máx./mín. (el precio promedio sigue llegando de la SFC) y la salud los muestra como "manual, sin leer reciente" sin marcar falla. Si la página que llega es el rechazo del CDN, la lectura se detiene en el acto (`BloqueoBVC`), no se reintenta y el otro conector de la Bolsa de esa misma corrida tampoco pide la página; queda captura y texto en `invest-data/diagnostico/`, igual que cuando la tabla no aparece por otra razón. El botón del aplicativo cuenta como lectura manual (marca `INVEST_LECTURA_MANUAL=1` solo en esa lectura).

**Quién escribe qué en `invest-data`.** GitHub Actions (cada noche, 19:17) y la tarea del PC (19:47) suben al mismo repositorio, así que nunca escriben el mismo archivo: las series macro van en una carpeta por fuente (`silver/fact_serie_macro/fuente=<id>/anio=<año>`, y `leer_tabla` las une); gold (`invest dims`, `invest factores`) solo se construye en el PC; la bitácora `meta/*.jsonl` se fusiona sumando líneas (`.gitattributes`, `merge=union`). La tarea del PC guarda sus cambios locales en un commit antes del `git pull --rebase`, y los dos lados reintentan el push si el otro subió algo entre tanto.

### Estados financieros de los emisores (`sfc_rnve_niif`)

`ingest/sfc_niif.py` lee la página "Informes financieros bajo NIIF y anexos" del SIMEV de la SFC para cada emisor de `config/emisores_rnve.yaml` (36 emisores con sus códigos SIMEV; algunos tienen varios porque la SFC los reclasificó). La página pide los datos con una clave propia del sitio que no se replica: Playwright elige año, tipo de informe y periodo como una persona y guarda las respuestas que la página recibe.

| Tabla silver | Qué trae | Cobertura |
| --- | --- | --- |
| `fact_anexo_sfc` | Anexos por celda (formato, fila, columna, valor): 141 información económica general (valor patrimonial y utilidad por acción, dividendos decretados, ventas, empleados), 211 acciones en circulación y accionistas, 529 principales accionistas, 058 subordinadas | Todos los emisores, trimestral |
| `fact_xbrl_sfc` | Hechos XBRL NIIF sin dimensiones (balance, resultados, flujos, por acción), del periodo y del comparativo, con corte y fecha de transmisión | Solo los emisores para los que el SIMEV publica el XBRL (Ecopetrol sí; Celsia, Cementos Argos y Grupo Argos no, nov-2026) |

Montos en miles de pesos; cifras por acción en pesos (columna `escala`). La carga inicial (2015 en adelante, unas 17 horas de navegador) va en tandas de 80 cortes por corrida (una hora): primero el último año, luego los cierres anuales hacia atrás y al final los demás trimestres; muestra el avance por emisor. Si la lista de informes trae XBRL pero la descarga falla, o el SIMEV responde un error en lugar del archivo (a veces entrega un JSON con `status: 500`), o el archivo baja pero no se puede leer (`ParseError`), los anexos del corte se guardan igual y el XBRL se reintenta hasta tres veces en corridas siguientes (`xbrl_pendiente`, `intentos_xbrl` y `xbrl_ilegible` en `meta/estado_fuentes/sfc_rnve_niif.json`). Canacol reporta en dólares (escala `miles_de_dolares`). Prueba de un emisor sin guardar: `invest probar-niif 260 36 2025-09-30`.

Primera carga (sep-2025, 23-nov-2026): 32 de 36 emisores con anexos y 8 con XBRL (Canacol, GEB, ISA, Promigas, ETB, Grupo Sura, Grupo Bolívar, Grupo Aval); sin nada Banco Popular, BBVA, Bolsa Mercantil y Davivienda Group. Revisión de orden de magnitud: activos consolidados de ISA 77,5 billones y de Grupo Aval 343,8 billones (miles de pesos × 1.000); falta cotejar cifra por cifra con los estados publicados. El XBRL de cada informe pesa unos 5 MB y no va al repositorio.

### Respaldo y cotejo de los fundamentales

El SIMEV publica XBRL (estados consolidados completos) de solo 10 de los 36 emisores. Para los demás, los anexos traen cifras por acción y acciones en circulación, pero no el balance ni el estado de resultados. El respaldo es Supersociedades (`supersociedades_10000`): sus cifras entran en `gold/fact_fundamental` con `base = separado_anual` y `fuente = supersociedades`, disponibles desde el 30 de junio del año siguiente, y de ahí salen `apalancamiento`, `roe_anual`, `margen_neto_12m` y el crecimiento de los ingresos de los emisores sin XBRL (los puntajes prefieren el consolidado y usan el separado anual cuando no lo hay).

`fundamentales.cotejo()` deja en `gold/fact_cotejo_fundamental`, por emisor y cierre, el patrimonio y la utilidad de los anexos (valor por acción × acciones en circulación) frente a los de Supersociedades: `ok` dentro de 2 % (patrimonio) o 10 % (utilidad, porque la utilidad por acción usa el promedio de acciones del año), o dentro de la precisión de Supersociedades; si no, `revisar`. Primer cotejo (24-nov-2026, 28 cierres de 2024 y 2025): el patrimonio cuadra al 0,5 % en 20; se apartan Grupo Sura (2024 y 2025) y Grupo Argos 2024 (escisiones de 2024–2025: acciones en circulación del anexo 211 distintas de las del valor patrimonial), Mineros 2024 e ISA 2024 (Supersociedades repite el patrimonio de 2023). La utilidad cuadra en la mayoría; se apartan Grupo Aval 2025, Terpel 2025 (utilidad por acción del anexo inconsistente) y los emisores cuyas acciones cambiaron en el año. Los "ingresos" del anexo 141 no son los ingresos operacionales (en GEB o ETB ni siquiera tienen el orden de magnitud), así que ya no se usan para métricas. Al cotejo se sumaron dos limpiezas de los anexos: menos de 100.000 acciones en circulación es una celda mal diligenciada (El Cóndor dic-2025 decía 1.367) y una utilidad por acción en cero exacto se descarta si otro anexo del mismo corte trae una cifra. Si dos anexos del mismo corte difieren, manda el 211 (después el 187 y el 141). Tres controles más quitan cifras por acción en otra escala (`fundamentales.descartar_fuente_discordante`, `descartar_saltos` y `descartar_upa_imposible`):
- un valor patrimonial por acción del 141 que se aparta más de 20 veces del 211 más cercano en el tiempo (BAC Holding: 128,41 contra 0,08 dólares);
- un tramo de hasta 8 cortes (dos años) que se aparta más de 20 veces del corte anterior y del siguiente, cuando esos dos coinciden y no hay un split entre medio (Paz del Río jun-2020 a sep-2022: 0,02 en seis cortes entre 19,95 y 21,24);
- una utilidad por acción mayor que 1,5 veces el valor patrimonial por acción del mismo corte o del más cercano (a menos de 200 días), que sería una rentabilidad de más de 150 % (BAC Holding sep-2025: 53,67 contra 0,07).

La utilidad por acción no pasa por la prueba de 20 veces porque es acumulada en el año y puede cambiar de signo. En los extremos de una serie no se descarta nada: ahí no se distingue un error de un cambio de nivel.

Cotejo de los consolidados del XBRL contra los estados publicados por las compañías (24-nov-2026, 31 cifras de activos, patrimonio y utilidad de la controladora 2024–2025): Ecopetrol, ISA, Grupo Sura, GEB, Grupo Bolívar y ETB coinciden exactas o por redondeo (GEB 2024 activos +0,16 %: el comparativo del informe 2025 parece reclasificado); las cifras son las contables, no las proforma que comunican algunas compañías (Grupo Sura). Error encontrado y corregido: BAC Holding transmite el XBRL con los montos en dólares sin escalar y se multiplicaban por 1.000 (ahora `xbrl_montos_en_unidades: true` en `config/emisores_rnve.yaml`), y un corte suyo había quedado marcado en pesos. La utilidad separada calculada como utilidad por acción × acciones al cierre falla cuando cambian las acciones en el año (Grupo Argos 2025: −9,9 % frente a lo publicado); por eso la utilidad directa de Supersociedades es la de referencia.

### Sucesiones (Bancolombia → Grupo Cibest)

`config/sucesiones.yaml` pega la historia de una acción que fue reemplazada sin venta (intercambio 1 a 1) al comienzo de la serie de su sucesora: BCOLOMBIA → CIBEST y PFBCOLOM → PFCIBEST (Deceval canceló las de Bancolombia el 16-may-2025; las de Cibest negocian desde el 19-may-2025). La SFC retiró a Bancolombia de su índice de especies, así que la historia llega por una de dos vías: el código SFC de la antecesora (`especie_sfc`; el conector de precios baja una vez sus archivos aunque no esté listada) o un CSV en `invest-data/fuentes_web/precios/` (fecha, precio, valor_transado opcional). `sucesiones.precios_sfc()` reemplaza la lectura directa de `silver/fact_precio_sfc` en retornos, liquidez, splits, escisiones, dividendos web y dimensiones; las filas heredadas llevan `fuente_id = sucesion:...`.

### Diciembres de los bancos desde el CUIF

El SIMEV no publica anexos de diciembre de Banco de Bogotá, Davivienda, Occidente, BBVA, Popular y AV Villas (ni de Corficolombiana, la BVC y Protección), 2016–2025. `fundamentales.desde_cuif` llena esos cortes con patrimonio (300000) o utilidad acumulada (590000) del CUIF del mismo mes sobre las acciones en circulación del anexo 211 más cercano, solo para bancos que no son holding y solo si en los cortes con anexo la cifra derivada repite la del anexo (mediana dentro de 2 %; Banco de Bogotá sep-2025: 47.021,41 en los dos). La utilidad por acción de Davivienda no pasa ese cotejo y no se deriva. `fuente = cuif_derivado`, disponible 50 días después del corte.

### Diciembres en el SIMEV como «Intermedios»

Los bancos, Corficolombiana, la BVC y Protección publican su informe de diciembre en el SIMEV como «Intermedios / Diciembre», no como «Cierre» (Banco de Bogotá dic-2024: «Cierre» devuelve la lista vacía, «Intermedios» trae los anexos; 24-nov-2026). `sfc_niif` prueba en diciembre primero «Cierre» y, si no trae anexos ni XBRL, «Intermedios». Los 112 diciembres marcados sin informe volvieron a la cola de la carga histórica. El CUIF sigue de respaldo para los bancos.

### Más dividendos web y estados de compañías (24-nov-2026)

Historiales de Investing para BBVA (ordinaria y preferencial), Popular, AV Villas (ordinaria y preferencial), Protección, Corferias, Paz del Río, El Cóndor, Bolsa Mercantil, Enka, Manufacturas de Cemento y Canacol: 401 pagos, 383 verificados solos y 18 decididos en la revisión (17 aprobados y la bonificación de 2008 de la Bolsa Mercantil rechazada). Fabricato no tiene dividendos en Investing. AV Villas, Protección, Corferias, El Cóndor y Bolsa Mercantil solo muestran los últimos 20 pagos. Canacol pagaba en dólares canadienses en Toronto: `src/invest/fx.py` pasa CAD a pesos con la TRM y la serie DEXCAUS de FRED (nueva), y retornos y dividendos web la usan también para los dólares. Canacol está en reestructuración (CCAA) con la negociación suspendida desde el 17-nov-2025 (`suspendida_desde` en `config/emisores_rnve.yaml`).

`config/estados_compania.yaml` suma Corferias (plantilla mensual), Paz del Río (cinco direcciones verificadas, `acepta_separados: true` porque no publica consolidados), Conconcreto (informe de gestión 2025) y El Cóndor (cierre 2023), con la nueva lista `directas` para sitios sin página índice legible. Enka, Fabricato y Manufacturas de Cemento quedan `pendiente`, a la espera de PDF puestos a mano.

### Cobertura de documentos (`invest cobertura`)

Escribe `revision/cobertura_documentos.csv`: por emisor, cortes NIIF con informe, sin informe publicado, de antes de cotizar y aún sin revisar; XBRL, PDF de la compañía, años de Supersociedades, dividendos (PDF e historiales web), inicio de la historia de precios, un diagnóstico y la fuente alterna propuesta. Solo conteos y fechas.

### Valor de la empresa, EBITDA y deuda

`metricas.py` arma, con los estados consolidados (XBRL de la SFC y, desde la versión 2 de su lectura, los PDF de cada compañía), la utilidad operacional y el EBITDA de 12 meses (utilidad operacional + depreciación y amortización del estado de flujos), los márgenes bruto, operacional y EBITDA, la deuda financiera (obligaciones financieras corrientes y no corrientes más los títulos emitidos que se reportan aparte; sin arrendamientos), la deuda neta (menos el efectivo), deuda neta / EBITDA y el interés minoritario. `fact_valoracion` suma el valor de la empresa (`ev` = capitalización de todas las clases del emisor + deuda neta + interés minoritario, en pesos con la TRM del día si el emisor reporta en dólares) y `ev_ebitda`. Nada de esto aplica a bancos, aseguradoras ni holdings financieros (`valoracion_empresa.excluir_financieros` en `config/convenciones.yaml`). El Screener y la comparación con el sector de la Ficha muestran EV/EBITDA, deuda neta / EBITDA y margen EBITDA.

Cotejo con datos reales (24-nov-2026): Ecopetrol deuda 120 billones a dic-2024 (incluye 94 de bonos), EBITDA de 12 meses 42,8 billones, deuda neta / EBITDA 2,3 y EV/EBITDA 5,1; ISA 3,7 y 8,1; ETB 1,5 y 2,1. GEB y Promigas tienen las cifras, pero su EBITDA de 12 meses necesita el cierre de dic-2025, que llega con la carga histórica de NIIF. El EBITDA de GEB y Grupo Argos no incluye los dividendos de sus asociadas (método de participación), así que su deuda / EBITDA se ve más alta que la que ellas publican.

### Estados consolidados publicados por la compañía (`estados_compania`)

Respaldo de los consolidados trimestrales y anuales de los emisores sin XBRL, empezando por los más líquidos. `src/invest/ingest/estados_compania.py` (fuente `estados_compania`, `ejecuta_en: pc`) lee `config/estados_compania.yaml`, donde está por emisor la página índice, los patrones o plantillas de URL, la moneda, las unidades esperadas y las notas de lo verificado.

1. **Descubre** los PDF: enlaces de las páginas índice (y las direcciones `.pdf` que vienen dentro del código de la página), plantillas de URL por corte y lo que se ponga a mano en `invest-data/fuentes_compania/<emisor_id>/`. Deja uno por corte (el de más prioridad; Ecopetrol une balance y resultados, que vienen en dos PDF).
2. **Baja solo lo nuevo** a `invest-data/anexos/companias/<emisor_id>/` (repositorio privado; `redistribuible: false`). Si un sitio responde 401/403 o con un desafío contra robots, no se esquiva: ese emisor se detiene, se anota en `meta/estado_fuentes/estados_compania.json` y no se vuelve a pedir en 7 días.
3. **Ubica las páginas** del estado de situación financiera y del de resultados (consolidados si el PDF trae también los separados) y, si cabe, del de cambios en el patrimonio; envía solo esas (máximo 8).
4. **Dos agentes con consenso**, como los dividendos (mismo cliente de la API, `dividendos.lectura_cruzada`): cada uno registra corte, si es consolidado, moneda, unidades del documento, meses acumulados de los resultados y los nueve conceptos (activos, pasivos, patrimonio total y de la controladora, efectivo, ingresos, utilidad total y de la controladora, utilidad básica por acción) con página y rótulo. Cada cifra se busca en el texto de las páginas enviadas; si no aparece (calculada o inventada), cuenta como diferencia. Hasta 2 rondas de revisión cruzada.
5. **Controles**: activos = pasivos + patrimonio total (± 1 %), patrimonio de la controladora ≤ total, signos (activos, ingresos y efectivo no negativos, efectivo ≤ activos, utilidad de la controladora y por acción con el mismo signo), moneda esperada, corte de fin de trimestre y resultados acumulados del año. Unidades distintas de las esperadas solo dan un aviso (Grupo Argos publicó mar-2026 en miles y dic-2025 en millones).
6. **Silver** `fact_ee_compania`: valor en pesos (`usd` para Mineros; por acción en `pesos_por_accion` / `usd_por_accion`), valor y unidad del documento, archivo, página del PDF original, fecha de publicación detectada (`Last-Modified` o fin del mes de la carpeta de carga) y estado. Lo que no tuvo consenso o falla un control va a `cuarentena/fact_ee_compania/` con su motivo (`sin_consenso`, `control_balance`, `control_controladora`, `control_signo`, `resultados_no_acumulados`, `moneda_inesperada`, `no_consolidado`, `sin_paginas`, `sin_texto`). Las lecturas completas de los dos agentes quedan en `silver/fact_ee_compania_extraccion`.

En `gold/fact_fundamental` entra como `base = consolidado`, `fuente = pdf_compania`, con `fecha_disponible` = fecha de publicación detectada o, si no se conoce, corte + 60 días (+ 90 en diciembre; `config/convenciones.yaml`), o la fecha en que se descubrió el PDF si fue antes. **Si para un emisor y corte hay XBRL, manda el XBRL** y no entra nada del PDF de ese corte. Ecopetrol está como emisor de control (`solo_cotejo: true`): no entra a `fact_fundamental`, y `gold/fact_cotejo_pdf_compania` compara lo leído contra el XBRL (`ok` dentro de 0,2 %, o 0,05 por acción; `revisar`; `sin_par`). Se puede hacer lo mismo con ISA u otro emisor con XBRL agregándolo al YAML.

| Emisor | Fuente (verificada el 24-nov-2026) | Formato y unidades | Estado |
| --- | --- | --- | --- |
| Grupo Éxito (CO-COG31) | Plantilla `grupoexito.com.co/es/estados/financieros/consolidados/<t>t<año>.pdf` (3t2025, 4t2025 y 3t2026 leídos) | PDF de estados consolidados condensados; millones de pesos; resultados acumulados y del trimestre | Automático |
| Grupo Nutresa (CO-COT04) | Índice `gruponutresa.com/en/investors/quarterly-results`; PDF en `wp-content/uploads/<año>/<mes>/` (`Grupo-Nutresa-EEFF-Consolidados-2025.pdf`, `...Consolidados-Junio.pdf` leídos) | PDF; millones de pesos; el nombre a veces no trae el año (se toma de la carpeta) | Automático |
| Grupo Argos (CO-COT09) | Índice `grupoargos.com/en/investor-relations/reports-and-presentations/` (enlaces armados con JavaScript); PDF en `files.grupoargos.com/uploads-grupo-argos/<año>/<mes>/` (mar-2026 y dic-2025 leídos) | PDF; miles o millones de pesos según el informe | Automático si el índice trae los enlaces; si no, a mano |
| Mineros (CO-COC07) | Índice `mineros.com.co/en/investors/financial-reports` (citado en su comunicado; no se pudo abrir desde aquí) | PDF en inglés; miles de dólares | Automático, por verificar en el PC |
| Cementos Argos (CO-COD38) | `argos.co/ir-2/`; los PDF (`Reporte-<t>T<aa>.pdf`) responden con desafío de Imperva Incapsula | Reporte trimestral con estados consolidados; millones de pesos | A mano en `fuentes_compania/CO-COD38/` |
| Celsia (CO-COT60) | `celsia.com` responde con desafío de Imperva Incapsula | Por confirmar | A mano en `fuentes_compania/CO-COT60/` |
| Terpel (CO-COG20) | Sin URL verificable | — | Pendiente (a mano si se consigue el PDF) |
| Ecopetrol (CO-COC04), control | Plantillas `files.ecopetrol.com.co/web/esp/estadosfinancieros/balance-gee<t>q<año>.pdf` y `pyg-gee-<t>q<año>.pdf` (y variantes con `t`) | Dos PDF por corte; miles de millones de pesos | Automático, solo cotejo |

**Límites y costo.** Por corrida: 6 documentos a la API, 10 páginas por documento (8 hasta el 24-nov-2026; las 2 nuevas son para el estado de flujos), 20 descargas, PDF de hasta 40 MB, 4 cortes hacia atrás en las plantillas (`limites` en el YAML). Con los precios del 24-nov-2026 (Sonnet 5.5 USD 2 / 10 y Opus 5.5 USD 4 / 20 por millón de tokens; 2.500 a 4.600 tokens por página de PDF), un documento cuesta unos USD 0,15 a 0,30 si hay consenso en la primera lectura y USD 0,50 a 1,00 con dos rondas de revisión; a lo sumo unos USD 6 por corrida y de USD 5 a 25 al año en régimen (7 emisores × 4 cortes). Enviar el PDF completo costaría de 10 a 30 veces más. Las disputas no se vuelven a enviar; los errores sí. Desde la versión 2 de la lectura (24-nov-2026) los agentes registran también utilidad bruta, utilidad operacional, depreciación y amortización (del estado de flujos), obligaciones financieras y bonos corrientes y no corrientes; un documento leído con la versión anterior se vuelve a leer una vez, después de los nuevos y dentro del tope de 6 por corrida (los 6 documentos ya leídos cuestan unos USD 2 a 4).

**Cómo correrlo en el PC** (también entra en `invest ingest --todas-pc` de la tarea diaria):

```powershell
$env:ANTHROPIC_API_KEY = "..."        # sin ella se bajan los PDF y la lectura queda pendiente
uv run invest ingest estados_compania
uv run invest dims                    # rehace fact_fundamental y el cotejo con el XBRL
```

**Sitios protegidos, lectura con ventana** (`ingest/estados_ventana.py`). Cementos Argos, Celsia, Nutresa y Grupo Argos bloquean a los programas (Imperva Incapsula, 401/403 o un desafío en la página índice; 24-nov-2026). No se esquiva: como con la Bolsa, la lectura la lanza Esteban (`uv run invest estados-ventana`, o el botón "Leer estados de compañías (con ventana)" en Salud de datos) y se abre una ventana normal de Chromium en las páginas de inversionistas de los emisores con `ventana: true` en el YAML. Si el sitio pide una verificación o un captcha, la resuelve él en la ventana; el programa solo espera a que aparezca la página real. Con la página abierta, guarda en `invest-data\fuentes_compania\<emisor_id>\` los PDF de estados consolidados enlazados (patrones del YAML; uno genérico si el emisor no tiene) y cualquier PDF que él abra mientras la ventana está abierta (`--abierta`, 120 s por emisor), con la misma sesión de la ventana y pausas entre descargas; después corre `estados_compania` para leerlos. Nunca va en una tarea programada (sin consola ni botón, se niega). Celsia no tiene página de informes verificada: se abre la portada y él navega hasta los estados.

También se puede bajar el PDF a mano en el navegador y guardarlo en esa carpeta con el corte en el nombre (p. ej. `Reporte-3T26.pdf`, `EEFF-Consolidados-Septiembre-2026.pdf`); la corrida siguiente lo toma (Terpel, sin URL conocida, va por aquí). Las notas de la corrida solo traen conteos. Pendiente: sumar más emisores sin XBRL y un archivo de revisión editable como el de dividendos.

El reporte de salud mide el rezago según la frecuencia de cada fuente (diaria 3 días hábiles, mensual 50, anual 360).

## Plantilla de bancos (`src/invest/bancos.py`, `config/bancos.yaml`)

Las métricas genéricas describen mal a un banco: el ROE de los anexos llega trimestral y por acción, y el apalancamiento (pasivos / activos) castiga lo que es su negocio (los depósitos). La plantilla arma `gold/fact_metrica_banco` (emisor × corte mensual × métrica, con `fecha_disponible`) con el CUIF mensual de la SFC y la usa en el factor calidad de los bancos y en la Ficha. Corre como paso `bancos` de `invest modelo`, antes de los puntajes.

**Datos.** El conector `cuif_datos_gov` pide, además del primer nivel (`XX0000`), las `cuentas_extra` del catálogo (cada una con su comentario). Los códigos van con 6 dígitos y ceros a la derecha, como en datos.gov.co (cuenta 1408 = `140800`, subcuenta `140810`); un código de 4 dígitos se completa solo. Cuando la lista trae cuentas que nunca se bajaron, la siguiente corrida sin `--desde` pide todo el histórico (desde 2015) una sola vez y lo anota en `meta/estado_fuentes/cuif_datos_gov.json`; a mano: `invest ingest cuif_datos_gov --desde 2015-01-01`. Mientras una cuenta no esté en silver, las métricas que la usan quedan vacías sin error (con la foto del 24-nov-2026, que solo tiene el primer nivel, salen ROE, ROA y utilidad de 12 meses). Fecha disponible = corte + `rezago_dias` (50) de la entrada del catálogo.

**Códigos del CUIF** (catálogo anexo a la Circular Externa 021 de 2014 de la SFC con sus modificaciones, compilado en [normograma.com](https://normograma.com/keralty/compilacion/docs/circular_superfinanciera_0021_2014.htm); la 4103 y la 5104 las modificó la [Circular Externa 033 de 2014](https://normograma.supersalud.gov.co/compilacion/docs/circular_superfinanciera_0033_2014.htm)):

| Agregado | Cuentas | Nota |
| --- | --- | --- |
| Activo, patrimonio, utilidad | 100000, 300000, 590000 | Primer nivel (ya se bajaban). 590000 es la utilidad neta acumulada del año |
| Intangibles | 1911 (191100) | Activos intangibles netos de amortización; incluye la plusvalía 191105 (se baja también, informativa) |
| Cartera bruta | 1404 vivienda y leasing habitacional, 1408 consumo, 1410 comercial, 1412 microcrédito | Capital con leasing financiero, antes del deterioro (el grupo 140000 es neto) |
| Cartera B a E | subcuentas 140415–140450 (B a E de vivienda y leasing habitacional), 140810–140825, 141010–141025, 141210–141225 | Calificación de riesgo de la SFC: B aceptable, C apreciable, D significativo, E incobrabilidad |
| Cartera C a E | las anteriores sin las de categoría B | |
| Deterioro acumulado | 1487 contracíclico individual, 1489 vivienda, 1491 consumo, 1493 microcrédito, 1495 comercial, 1498 general | Cuentas correctoras: se suma el valor absoluto |
| Depósitos | 2105 cuenta corriente, 2106 simples, 2107 CDT, 2108 ahorro, 2109 ahorro especial, 2110 CDAT de valor real, 2113 fondos en fideicomiso y cuentas especiales, 2116 especiales, 2117 exigibilidades por servicios, 2118 servicios de recaudo, 2120 electrónicos | Depósitos y exigibilidades |
| Ingresos por intereses | 4102 ingresos financieros cartera, 4103 mercado monetario y otros intereses | Acumulados del año |
| Gastos por intereses | 5102 depósitos y exigibilidades, 5103 créditos de bancos y otras obligaciones, 5104 mercado monetario y otros intereses | Acumulados del año |
| Gastos de personal y administrativos | 5120 beneficios a empleados, 5130 honorarios, 5140 impuestos y tasas, 5145 arrendamientos, 5150 contribuciones y afiliaciones, 5155 seguros, 5160 mantenimiento y reparaciones, 5165 adecuación, 5175 depreciación, 5180 amortización de intangibles, 5190 diversos | Ojo: la 5160 es mantenimiento, no deterioro |
| Deterioro del periodo | 5170 total y sus subcuentas 517005 cartera, 517010 leasing financiero; 5171 contracíclico total y 517105 consumo, 517115 comercial | El gasto de deterioro de cartera es la 5170, no la 5160 |
| Recuperaciones | 4198 total y 419810 cartera, 419815 leasing financiero, 419825 y 419835 contracíclico, 419855 cartera castigada recuperada | Se restan del deterioro |
| Otros | 120000 mercado monetario (activo), 410000 ingresos, 510000 gastos de operación | Primer nivel |

**Fórmulas.** Resultados de 12 meses = acumulado del año + diciembre anterior − mismo mes del año anterior (`metricas.doce_meses`); "promedio" = promedio de los 13 cierres mensuales t−12 a t (si falta uno, la métrica queda vacía).

| Métrica | Fórmula |
| --- | --- |
| `roe_banco_12m`, `roa_banco_12m` | utilidad 12m / patrimonio promedio; / activo promedio |
| `rote_12m` | utilidad 12m / patrimonio tangible promedio (patrimonio − intangibles 1911) |
| `nim_12m` | (4102 + 4103 − 5102 − 5103 − 5104) 12m / promedio de (cartera bruta + 120000). Aproximación: deja fuera el rendimiento de las inversiones (sus cuentas, 4104 a 4111, mezclan intereses con valoración a mercado) y el leasing operativo (4196), así que subestima el margen de los bancos con mucho portafolio o leasing |
| `eficiencia_12m` | gastos de personal y administrativos 12m / margen financiero bruto 12m, con margen financiero bruto = (410000 − 4198) − (510000 − gastos administrativos − 5170 − 5171) = utilidad antes de impuestos + gastos administrativos + deterioro neto total. Incluye impuestos operativos (4x1000, ICA) y contribuciones |
| `costo_riesgo_12m` | (517005 + 517010 + 517105 + 517115 − 419810 − 419815 − 419825 − 419835 − 419855) 12m / cartera bruta promedio |
| `indice_cartera_riesgo` | cartera B a E / cartera bruta (el "indicador de calidad" que usa el Banco de la República: cartera riesgosa B, C, D y E sobre cartera bruta, [Informe especial de Estabilidad Financiera, sep-2013](https://www.banrep.gov.co/es/print/pdf/node/33647)) |
| `indice_cartera_cde`, `cobertura_cartera_cde` | cartera C a E / cartera bruta; deterioro acumulado / cartera C a E. El CUIF no separa la cartera vencida por días de mora: C a E es la aproximación (el mismo informe del BanRep llama "vencida" a la calificada C, D y E). No es el indicador de cartera vencida que publica la SFC |
| `crecimiento_cartera_12m`, `crecimiento_depositos_12m`, `cartera_depositos` | variación frente al mismo corte del año anterior; cartera bruta / depósitos |
| `cartera_bruta`, `depositos`, `patrimonio_tangible`, `utilidad_12m` | saldos en pesos |

**Entidades y emisores** (`mapeo` en `config/bancos.yaml`, no en el código): Bancolombia (7) → CO-COTK7, el emisor que reúne la historia de Bancolombia y desde el 19-may-2025 es Grupo Cibest ([Valora Analitik](https://www.valoraanalitik.com/grupo-cibest-holding-de-bancolombia-comienza-a-cotizar-en-las-bolsas-de-colombia-y-nueva-york/)); Davivienda (39) → Banco Davivienda (CO-COB51) y Davivienda Group (CO-PAK01), que cotizan en paralelo desde el 21-nov-2025 ([Valora Analitik](https://www.valoraanalitik.com/davivienda-group-completa-transformacion-holding-exitosa-emision-acciones/)); Bogotá (1) → CO-COB01, Popular (2) → CO-COB02, BBVA (13) → CO-COB13, Occidente (23) → CO-COB23, AV Villas (49) → CO-COB52; Grupo Aval (CO-COT29) = suma simple de Bogotá, Popular, Occidente y AV Villas (`base = cuif_suma_bancos`, solo los meses en que reportan los cuatro); GNB Sudameris (12) no tiene acción en `dim_activo` y queda como `CUIF-12`, solo par de comparación. **Holdings:** el CUIF es el estado separado del banco, no el consolidado del holding (Cibest tiene además Banistmo, Banco Agrícola y BAM; Davivienda Group, sus filiales del exterior; Aval, Corficolombiana y Porvenir). `es_holding` lo marca en cada fila (en Cibest, desde el 19-may-2025) y la Ficha lo dice.

**Puntajes** (sección `bancos` de `config/escala.yaml`). Es banco el emisor del sector `BC-ESTABLECIMIENTO BANCARIO` o con cifras en `fact_metrica_banco` (así entran los tres holdings, que la SFC clasifica como sociedades inversoras). Para ellos el factor calidad usa `rote_12m` (más es mejor; mientras falte, su respaldo `roe_banco_12m`), `costo_riesgo_12m` y `eficiencia_12m` (menos es mejor) en lugar de `roe_12m` y `apalancamiento`; los demás emisores siguen con las genéricas y su referencia ya no incluye a los bancos. Comparación entre pares: las métricas de banco se comparan solo entre bancos, con grupo `bancos` en la auditoría; la referencia (mediana y desviación absoluta mediana) son las 8 entidades individuales del CUIF con su último corte disponible a la fecha (hasta 100 días), tengan o no acción líquida, contando una vez a Davivienda aunque tenga dos emisores y sin la suma de Aval. Con menos de 5 bancos con dato (`minimo_pares`) la métrica queda vacía y, si no queda ninguna, el factor calidad del banco queda en N/D (nunca 5). La regla general de grupos sigue para los demás factores: el sector bancario nunca reúne 8 acciones en el universo líquido, así que valor, crecimiento, momentum y riesgo de los bancos se comparan con todo el universo (los holdings, con las sociedades inversoras si ese sector llega a 8). `fact_matriz` trae `es_banco`, `banco_holding` y las métricas de banco del puntaje.

**Ficha.** `GET /api/ficha/<especie>/banco` (`tiene: false` si el emisor no tiene cifras) y la sección "Banco": último corte y el de un año antes, 5 años de ROTE/ROE, NIM, costo del riesgo y cartera en riesgo, y la tabla de los bancos del CUIF (exportable).

**Con la foto del 24-nov-2026** (corte sep-2026, conocido desde el 19-nov-2026; ROE de 12 meses del banco): GNB Sudameris 54,9 % (salto de la utilidad en el cuarto trimestre de 2025: el acumulado pasó de 0,42 billones en septiembre a 1,86 billones en diciembre), Bancolombia 29,0 % (su patrimonio separado bajó de 39,4 a 22,7 billones entre mar y jun-2025, en la reorganización de Grupo Cibest; un año antes, 17,3 %), Davivienda 15,3 %, BBVA 13,7 %, Occidente 10,3 %, Bogotá 7,9 %, Aval (suma) 7,5 %, Popular 2,2 % y AV Villas 1,5 %; mediana de los 8 bancos 12,0 %.

**Límites.** Estados separados: la utilidad incluye lo que el banco gana en sus filiales por el método de participación y la plusvalía de esas filiales sigue dentro de la inversión (no en 1911), así que el ROTE de un banco con filiales grandes se parece a su ROE. Las partidas extraordinarias (ventas de filiales, reorganizaciones como la de Cibest, fusiones como Davivienda con Scotiabank) mueven las métricas de 12 meses durante un año. NIM y cartera vencida son aproximaciones (ver fórmulas). Los códigos del CUIF cambian por circular: si la SFC crea o elimina una cuenta, se ajusta `config/bancos.yaml` y `cuentas_extra` (una prueba verifica que coincidan). Las cifras de la cartera por calificación y el deterioro aún no se han cotejado con los informes de cada banco: `tests/test_bancos.py` verifica, cuando lleguen, que cartera bruta − deterioro ≈ grupo 14.

### Bandas de valoración (`src/invest/bandas.py`, `config/bandas.yaml`)

Ubica el P/VL y el P/U de cada acción dentro de su propia historia, como la gráfica clásica de P/VL con su media y ±1 y ±2 desviaciones estándar. Es descriptivo: dice si la acción está por encima o por debajo de su media histórica, no si conviene tenerla. Corre como paso `bandas` de `invest modelo`, después de `metricas`.

- **Serie diaria** (`gold/fact_valoracion_diaria`): cada día con negocios, el precio sobre el valor patrimonial y la utilidad por acción de 12 meses **vigentes ese día** (as-of por `fecha_disponible`, con las mismas reglas de `fact_valoracion`: estados separados, cifras del emisor también para las preferenciales, nulo si el corte tiene más de 15 meses, dólares a pesos con la TRM del día). P/U nulo si la utilidad es ≤ 0; P/VL nulo si el patrimonio por acción no es positivo.
- **Bandas** (`gold/fact_banda_valoracion_mensual`, en las fechas de `fact_valoracion`, y `gold/fact_banda_valoracion`, la foto del último día): por especie × indicador × ventana (1a, 3a, 5a, 10a = [fecha − N años, fecha]) el valor actual (último día con negocios, de hace 30 días o menos), media, desviación estándar muestral, z = (actual − media) / desviación, percentil del actual entre los días de la ventana, mínimo y máximo, `n_obs`, `cobertura` = días con dato / sesiones XBOG de la ventana y `fecha_desde`. Con cobertura menor que 60 % (`cobertura_minima`) z y percentil quedan nulos. Cada fila usa solo datos con fecha ≤ la suya: la serie mensual sirve para medir después si z anticipa retornos (no está validado).
- **Quiebres** (`gold/fact_quiebre_valoracion`): escisiones y repartos de acciones de `config/eventos_capital.yaml`, splits o reagrupaciones de `gold/fact_split` y, solo para el P/VL, los días en que el valor patrimonial por acción vigente se multiplica o divide por más de 3 (`salto_vpa`: split sin registrar, cambio en las acciones o error de la fuente). El último dentro de la ventana queda en `quiebre`, `tipo_quiebre` y `aviso`. Con `recortar_al_quiebre: true` la ventana empieza en el quiebre (la cobertura se sigue midiendo sobre la ventana completa).
- **Screener** `valoracion_historica_baja`: z del P/VL a 5 años ≤ −1, sin quiebre en la ventana y en el universo líquido (`fact_matriz` trae `z_p_vl_5a`, `quiebre_p_vl_5a`, `z_p_u_5a` y `quiebre_p_u_5a`).
- **Aplicativo:** página Valoración (`#/valoracion`) y panel de la Ficha; `GET /api/valoracion/bandas?indicador=p_vl&ventana=5a` (todas las especies) y `GET /api/ficha/<especie>/bandas?indicador=p_vl&ventana=5a` (serie diaria de la ventana, la banda y los quiebres; acepta nemotécnico y responde `tiene: false` si no hay bandas). Explicaciones en `config/variables/` (`z_p_vl`, `z_p_u`, `bandas_valoracion`, borradores).

Con los datos del 24-nov-2026 (último precio 23-nov): 34 especies con P/VL, 17 con z a 5 años (las demás tienen poca cobertura: pocos días con negocios, Cibest y Davivienda Group listadas desde 2025, o cifras por acción incompletas). Lo más alto son Grupo Sura (+3,5), su preferencial (+2,1) y las de Grupo Argos (+1,7 y +1,9), todas con el aviso de la escisión del 28-jul-2025: el precio cayó ese día y el valor patrimonial por acción publicado tardó en reflejarlo, así que su historia de antes no es comparable. Lo más bajo es BHI (−1,9), pero por un salto de 1.554 veces en su valor patrimonial por acción del anexo de mar-2026 (128,41 "dólares por acción", cuando antes era 0,08): queda marcado como `salto_vpa` y es un dato por revisar en la fuente. Sin quiebre, Promigas (−0,6) y Ecopetrol (−0,3) son las únicas por debajo de su media de 5 años; ese día ninguna acción cumple el screener (desde 2020 lo cumplieron 14 especies en algún fin de mes; la última, Ecopetrol, de abr a oct-2025).

## Factores y validación (`src/invest/factores.py`, `invest factores`)

Cada fin de mes (última sesión XBOG), con lo que se sabía a esa fecha: universo de acciones líquidas con al menos 250 días de negocios y un negocio en las últimas 10 sesiones; factores de `config/factores.yaml` sobre un índice de retorno total que no cuenta los saltos atípicos. Salidas: `gold/fact_factor_mensual` y `gold/fact_validacion_factor`.

Validación por factor, con el signo de su hipótesis: IC de Spearman mensual contra el retorno del mes siguiente, t de Newey-West, corrección de Holm por probar varios factores, spread entre el tercio superior y el inferior, y muestra de descubrimiento (2008-2017) contra fuera de muestra (2018 en adelante). Solo un factor `validado` entra en la matriz de factores (`invest ficha`).

Primera corrida (23-nov-2026, 223 meses, mediana de 16 acciones por mes): **validados** momentum 12-1 (IC 0,061, t 3,2, positivo en 15 de 19 años) y rendimiento por dividendo de 12 meses (IC 0,064, t 3,8, positivo en 14 de 19 años); se sostienen con 6 rezagos de Newey-West, desde 2010 y con un mínimo de 12 acciones por mes. No validados: momentum 6-1, reversión de un mes, baja volatilidad y liquidez. El spread de tercios es de unos 0,6 % mensual (Sharpe anual cercano a 0,4) antes de costos de transacción. **Con costos** (0,5 % por compra o venta, `costo_ida` en `config/factores.yaml`) y solo comprando, que es lo realista en la BVC: el tercio superior le gana al promedio del universo por 0,4 % mensual bruto y 0,1 % neto en momentum (rota 27 % al mes) y 0,2 % bruto y casi 0 neto en dividendo (rota 18 %). Los dos factores casi no se solapan (correlación media 0,15); su combinación tiene IC 0,077 (t 4,2) y 0,2 % mensual neto, pero es informativa porque se armó después de ver los resultados. La correlación entre factores queda en `gold/fact_correlacion_factor`. Conclusión: los factores ordenan, pero la ventaja que queda después de costos es pequeña.

## Matriz de factores (`src/invest/ficha.py`, `src/invest/tablero.py`, `invest ficha`)

Herramienta educativa: ordena las acciones del universo con los factores que quedaron `validado`; no emite señales de compra o venta. Si un factor deja de validarse (la validación se rehace cada día), sale del puntaje solo.

- **Puntaje** (`gold/fact_puntaje_mensual`): promedio de los percentiles de los factores validados, cada uno orientado según su hipótesis; posición y tercio dentro del mes. Es exactamente el `combinado_validados` de la validación (mismo orden de desempate), así que la evidencia que acompaña al tablero corresponde a lo que se muestra.
- **Historia** (`gold/fact_puntaje_tercio`): por mes, el retorno siguiente del tercio superior y del universo con pesos iguales, la rotación del tercio y el exceso neto de costos. Su promedio coincide con `exceso_superior` y `exceso_superior_neto` de la validación (lo verifica una prueba).
- **Ficha** (`gold/fact_ficha_accion`): al último fin de mes (o la última sesión del mes en curso), una fila por acción activa con puntaje, cambio frente a 1 y 3 meses, meses seguidos en el tercio superior, valor y percentil de cada factor (los no validados quedan como contexto), retorno de 12 meses, volatilidad, monto negociado, P/VL (último valor patrimonial publicado) y P/U (utilidad por acción del último cierre anual, solo si es positiva), los avisos de calidad de datos y, para las que están fuera del universo, el motivo.
- **Tablero** (`invest-data/tableros/matriz_factores.html`): un solo archivo, sin conexión a internet ni librerías externas; los datos van dentro y no salen del computador. Matriz de los dos factores validados, detalle por acción (percentil del puntaje y retorno acumulado de los últimos 36 meses), tabla ordenable, historia del tercio superior contra el universo (bruto y neto), evidencia de cada factor y acciones fuera del universo. Modo claro y oscuro según Windows. No se versiona (`.gitignore` de `invest-data`): la tarea diaria lo regenera después de `invest factores`. Nombres cortos en `config/tablero.yaml`.

Con los datos al 18-nov-2026: 18 acciones con puntaje; desde 2008 el tercio superior le gana al universo por 0,47 % mensual bruto y 0,21 % neto de costos, con t de Newey-West de 1,0 en el neto, así que esa ventaja todavía no se distingue del azar.

## Modelo de datos del aplicativo (`invest modelo`, `config/modelo.yaml`)

El aplicativo lee una sola base, `invest-data/modelo.duckdb`, con una vista por tabla del catálogo `config/modelo.yaml` (capa, ruta, grano, claves, columnas y la página que la usa) y vistas de conveniencia (`v_precio_ajustado`, `v_ficha_actual`, `v_mapa_mercado`). La base no se versiona: guarda rutas de este computador y se rehace en segundos; si el aplicativo la tiene abierta, queda la anterior. `invest modelo` verifica el contrato (cada tabla existe, trae sus columnas con un tipo compatible y sus claves no se repiten) y la tarea diaria escribe los problemas en la bitácora.

**Recálculo incremental.** `invest modelo` no rehace lo que no cambió. Cada paso (técnico, riesgo, métricas, bandas, bancos, puntajes, calendario, cartera, variables) declara en `PASOS` (`src/invest/modelo.py`) las tablas que lee y escribe; antes de correrlo se compara la huella de sus insumos (contenido de esos Parquet, de sus archivos de `config/` y de su propio código; para calendario y cartera, también la fecha del día) y de sus salidas con la de su última corrida exitosa. Si no cambió nada, el paso se salta y la salida lo dice (`tecnico: saltado (sin cambios en sus insumos)`); si un paso se recalcula, también los que leen sus salidas (por ejemplo, precios nuevos rehacen técnico, métricas, bandas y cartera, y por arrastre riesgo y puntajes; un cambio en las transacciones solo rehace la cartera). La base `modelo.duckdb` y el contrato se rehacen siempre (segundos). Las huellas quedan en `invest-data/.modelo/huellas.json`, junto a la base y fuera de Git (la carpeta trae su propio `.gitignore`); si se borran o se dañan, la siguiente corrida recalcula todo. La huella es por contenido: reescribir un archivo igual no dispara nada, y un archivo solo se vuelve a leer si cambió su tamaño o su fecha. Un paso que falla no guarda huella y se repite en la corrida siguiente. `invest modelo --todo` fuerza todo (por ejemplo, después de cambiar una librería). Con los datos de demostración, la segunda corrida sin cambios pasa de unos 28 s a 2 s. Una prueba compara `PASOS` con las lecturas y escrituras del código de cada módulo, para que la lista no se desactualice.

| Bloque | Tablas (gold) | Módulo |
| --- | --- | --- |
| Emisores y precios | `dim_emisor`; `fact_precio_diario`: precio SFC + cierre/máximo/mínimo de la Bolsa cuando hay lectura, retorno total, acciones en circulación conocidas a la fecha (anexo 211, por clase) y capitalización en pesos y dólares | `mercado.py` |
| Macro | `dim_indicador_macro` (`config/indicadores_macro.yaml`) y `fact_macro`: TRM, tasas BanRep, IPC, FRED, índices de la Bolsa e inflación anual y tasa real derivadas, con fecha de publicación | `macro.py` |
| Técnico | `fact_indice_mercado` (universo líquido con pesos iguales y por capitalización, en pesos y dólares) y `fact_tecnico_diario` (medias de 50 y 200, RSI, MACD, Bollinger, caída desde el máximo de 52 semanas, retornos, fuerza relativa; N/A si no es líquida) | `tecnico.py` |
| Riesgo | `fact_riesgo`: volatilidad, beta, correlación, Sharpe, Sortino, drawdown, VaR y CVaR a 1, 3 y 5 años, semanal, en pesos y dólares | `riesgo.py` |
| Fundamentales | `fact_metrica_fundamental` (VPA, UPA y ROE de 12 meses, payout, crecimiento, apalancamiento, margen; con `fecha_disponible`) y `fact_valoracion` (P/VL, P/U, rendimiento de la utilidad y del dividendo a cada fin de mes) | `metricas.py` |
| Bandas de valoración | `fact_valoracion_diaria`, `fact_quiebre_valoracion`, `fact_banda_valoracion` (hoy) y `fact_banda_valoracion_mensual` (historia): P/VL y P/U frente a su propia media y desviación en 1, 3, 5 y 10 años (ver "Bandas de valoración") | `bandas.py`, `config/bandas.yaml` |
| Bancos | `fact_metrica_banco`: plantilla de bancos con el CUIF (ver "Plantilla de bancos") | `bancos.py`, `config/bancos.yaml` |
| Puntajes | `fact_puntaje` (auditoría: valor, z, percentil y pares de cada métrica y factor), `fact_matriz` (1 a 10 por factor, ejes, cuadrante; decisión vacía mientras no esté validada) y `fact_screener_corrida` (`config/screeners.yaml`) | `puntajes.py`, `config/escala.yaml` |
| Calendario | `fact_evento_calendario`: fechas ex y de pago, asambleas, decisiones del BanRep (cambios de tasa y `config/calendario_macro.yaml`), IPC | `calendario_eventos.py` |
| Usuario | `invest-data/usuario/`: perfiles con versiones, asignación objetivo, carteras, transacciones, tesis, parámetros tributarios (vacíos: con el contador) y costos; se escriben validados | `usuario.py` |
| Cartera | `fact_posicion` (costo fiscal promedio, valor, peso), `fact_asignacion` (desvío frente a las bandas) y `fact_desempeno` (Dietz modificado, TWR, XIRR, benchmark) | `cartera.py` |
| Variables | `dim_variable`: fórmula, origen y explicación breve de 61 variables (`config/variables/`, borradores por revisar) | `variables.py` |

**Revisión de páginas y salidas (24-nov-2026).** Correcciones tras revisar cada página con los datos reales:

- *Técnico en acciones poco líquidas.* Los retornos de 1, 3, 6 y 12 meses, el máximo y mínimo de 52 semanas y la fuerza relativa usan días calendario (el último negocio de hace 30, 91, 182 o 365 días, si no es más viejo que 10, 20, 30 o 45 días); antes contaban 252 negocios, que en una acción que negocia pocas veces abarcan años (la Bolsa Mercantil salía con +898 % "a 12 meses"; es +64 %). Las medias de 50 y 200, el RSI y el MACD siguen contando negocios, pero si la acción no es líquida ese día la distancia a la media de 200 no entra al factor momentum ni al global, y la ficha no la muestra ni dibuja la media.
- *Mínimo de factores para el global* (`escala.minimo_factores_global: 3`): con menos factores con dato el global queda N/D. Una acción con solo el rendimiento por dividendo ya no queda primera del aplicativo.
- *Valor patrimonial por acción aislado* (`descartar_punto_aislado`): un tramo de hasta 3 cortes seguidos que se aparta 1,5 veces o más de los dos vecinos que lo rodean, cuando esos coinciden (a menos de 20 %, a menos de 200 días más 100 por cada corte de más) y sin split entre medio, se descarta. Grupo Sura mar-2026: 87.310 entre 42.455 y 42.453; BVC mar y jun-2019: 825 y 4.640 entre 8.167 y 8.156. Una escisión que no vuelve se conserva, porque sus vecinos no coinciden.
- Los dos filtros se repiten sobre la serie separada completa (anexos + CUIF de bancos): el primer anexo de BBVA (mar-2017, 26.537) solo tiene vecinos derivados del CUIF (278,76). Tras la carga NIIF completa quitaron 10 cifras (BBVA 1, Paz del Río 7, BVC 2) y ninguna otra; la validación de factores no cambió (valor en libros/precio sigue sin validar, IC −0,024 en 136 meses).
- *Salud de datos* se evalúa dentro de `invest modelo`, antes de armar la base: la página ya no queda una corrida atrás ni le faltan fuentes (NIIF, Supersociedades, estados de compañías).
- *Calendario.* La ficha muestra solo los eventos de su especie (y los del emisor sin especie), no los de su otra clase; "Próximos 45 días" agrega el nemotécnico y la moneda cuando no es COP (Mineros paga en dólares).
- *Aprender:* las variables relacionadas salen con su nombre y enlace (antes, la dirección cruda).
- *Carteras y tesis:* el historial y las tesis muestran nemotécnico y nombre; los formularios aceptan el nemotécnico o el nombre (el servidor lo convierte a la especie).
- *Tablas anchas:* la primera columna queda fija al desplazarse a la derecha. *Mi cartera* sin perfil muestra la composición en vez de un "desvío" sin objetivo. `/tablero` sin generar explica cómo generarlo.

**Segunda tanda (24-nov-2026, noche).**

- *Utilidad y crecimiento a través de una escisión o una sucesión* (`metricas.sin_eventos_de_estructura`): la utilidad de 12 meses que incluye una escisión, un reparto de acciones de otra sociedad (`config/eventos_capital.yaml`) o el comienzo de una sucesora (`config/sucesiones.yaml`) queda vacía (UPA y utilidad de 12 meses, ROE, margen neto, payout), y el crecimiento que compara a través de uno de esos eventos (24 meses) también. La fila se conserva con valor nulo para que el as-of no recupere un corte más viejo. Cementos Argos 2025 (UPA 2.084 contra 155 en 2024, por la escisión de jul-2025) y Grupo Argos (ROE 38 %) ya no salen con P/U de 4,6x y 2,4x ni con crecimiento de 10; Cibest no muestra un P/U de 21x calculado con su primer año incompleto.
- *Valor patrimonial del 141 "con valorización"*: el 141 trae dos valores; el "con" coincide con el 211 en todos los emisores, el "sin" difiere 7 a 22 % en Grupo Bolívar, El Cóndor y Grupo Cibest (el P/VL de Bolívar saltaba cada trimestre desde 2017). Manda el "con"; el "sin" queda de respaldo.
- *Anexos descartados por emisor* (`fundamentales.descartar_anexos` en `config/convenciones.yaml`, con su motivo): la utilidad por acción del 141 de Grupo Cibest (9.355 contra 4.320 del 211 en jun-2026; 14.338 a sep-2026).
- *Sector por negocio* (`config/sectores.yaml`, columna `sector` de `dim_emisor`): screener, valoración, mapa, ficha y su "Comparación con su sector" agrupan por lo que hace la empresa (Bancos, Servicios públicos, Petróleo, gas y combustibles, Materiales…) y no por el tipo de entidad de la SFC. El puntaje de factores sigue usando el sector de la SFC.
- *Pares por métrica*: si en el sector hay menos de `minimo_pares_metrica` (5) acciones con una métrica, esa métrica se compara con el universo (antes quedaba sin puntaje: el crecimiento de Celsia cuando a los demás holdings se les anuló).
- *Crecimiento de los bancos*: cartera y depósitos de 12 meses frente a los bancos del CUIF (antes N/D).
- *Interfaz*: rótulos de la matriz sin choques (si no caben, el nombre va en el tooltip); nemotécnico al final de las líneas del graficador; recuadros pequeños del mapa con nombre recortado; la variación del MSCI COLCAP dice de qué sesión es; la información relevante se recorta en palabra completa; el tablero de factores abre con tema oscuro desde el aplicativo; la nota de la SFC aclara cuando se cargó la historia de una antecesora sin reporte nuevo.

**Calculadora de rebalanceo (25-nov-2026).** En *Mi cartera*, si la cartera tiene un perfil con asignación objetivo, el panel "Calculadora de rebalanceo" (`src/invest/rebalanceo.py`, `GET /api/cartera/<id>/rebalanceo?aporte=&modo=&comision=`) calcula cuánto mover en cada clase para volver al objetivo, con un aporte o retiro opcional: *volver al objetivo* (puede vender) o *solo con el aporte* (no vende: usa el efectivo que sobra frente a su objetivo y el aporte; un retiro sale primero del efectivo). Dentro de cada clase el monto se reparte entre las posiciones que ya tiene, en proporción a su valor, en acciones enteras (el residuo va a la que más perdió al redondear); no elige acciones nuevas. Estima la comisión, avisa si un emisor queda por encima del máximo por emisor del perfil y si una clase objetivo no tiene posiciones. Es aritmética sobre el objetivo del usuario, no una recomendación.

**Alertas (25-nov-2026).** Página *Alertas* (con el número de las que piden atención junto al menú), `GET /api/alertas` e `invest alertas [--nuevas] [--json]` (`src/invest/alertas.py`). Se calculan al momento sobre `modelo.duckdb`:

- *Para atender*: una clase de una cartera fuera de su banda; un emisor por encima del máximo por emisor del perfil; una acción que tiene cayó 5 % o más en su último día de negocio; la revisión de una tesis vencida; una alerta propia que se cumplió.
- *Para saber*: fecha ex-dividendo en los próximos 7 días de una acción que tiene; precio objetivo de una tesis alcanzado; P/VL a 2 desviaciones o más de su media de 5 años (acciones que tiene o con tesis, sin quiebre); una fuente de datos atrasada o con error (salvo las manuales).
- *Alertas propias* (`usuario/dim_alerta`): precio, P/VL, z del P/VL o rendimiento por dividendo de una acción frente a un umbral; se activan, desactivan y borran desde la página.

La tarea diaria del PC (`scripts/windows/ejecutar_pc.ps1`) corre `invest alertas --nuevas --json` al final, las anota en la bitácora y muestra una notificación de Windows con las nuevas. Una condición que sigue vigente (fuera de banda, tesis vencida) se avisa una vez y otra vez solo si desaparece y vuelve; los hechos puntuales (una caída, una fecha ex) se avisan cada vez. Lo ya avisado queda en `~/.invest/alertas_vistas.json`, fuera del repositorio de datos. Son avisos para mirar, no recomendaciones.

**Carga NIIF de corrido** (`scripts\windows\carga_niif.ps1`). Cada `invest ingest sfc_rnve_niif` revisa hasta 80 cortes y termina (o se detiene antes si el SIMEV falla 8 veces seguidas). El script encadena corridas hasta que no quedan pendientes: mantiene despierto el PC, espera 10 minutos tras una corrida detenida por el SIMEV, para si tres corridas no avanzan o el SIMEV falla en tres seguidas, guarda los datos (commit) después de cada corrida y al final corre `invest dims`, `invest modelo` y sube a GitHub. Se puede cerrar en cualquier momento: la siguiente vez sigue donde iba.

**GitHub Actions.** `invest salud --solo actions`: el paso "Salud de datos" de la ingesta en la nube solo falla por fuentes que corren en Actions; una fuente del PC atrasada (la carga NIIF) sale como aviso. Las acciones de GitHub pasan a `checkout@v5`, `setup-python@v6` y `upload-artifact@v5` (Node 24).

Pendiente de datos, no de código: la historia de la tasa BanRep (la primera carga quedó recortada; el conector la completa solo en su próxima corrida), la utilidad de 12 meses y el ROE (cuando la carga histórica de NIIF traiga los cierres anuales), la historia del MSCI COLCAP (mientras tanto la beta es contra el universo líquido) y las métricas de bancos que usan las cuentas nuevas del CUIF (ROTE, NIM, eficiencia, costo del riesgo, cartera: llegan con la próxima corrida del conector).

## Aplicativo local (`src/invest/app/`, `invest app`)

Doble clic en `scripts\abrir_app.cmd` (o `uv run --extra app invest app`): abre el navegador en `http://127.0.0.1:8790/` (el 8765 lo usa el aplicativo de seguimiento financiero de la SDIS). Si el puerto lo ocupa otro programa, usa el siguiente libre y lo dice en la ventana; si el aplicativo ya está abierto, solo abre el navegador. Solo escucha en este computador y solo atiende pedidos dirigidos a `127.0.0.1` o `localhost`; los cambios (guardar, lanzar acciones) exigen el encabezado `X-Invest` que pone la propia página, y se rechazan los pedidos que el navegador marca como de otro sitio. Así otra página abierta en el mismo navegador no puede lanzar acciones ni leer la cartera. Lee de `modelo.duckdb` (una conexión de solo lectura por consulta) y escribe únicamente en `invest-data/usuario/`, validando antes de guardar; tras cada cambio de carteras o transacciones recalcula posiciones y desempeño.

| Página | Qué muestra |
| --- | --- |
| Mercado y país | TRM, tasa BanRep, inflación, COLCAP, Tesoro; mapa del mercado por capitalización (color: retorno de 1 o 12 meses o puntaje; filtro de acciones líquidas); movimientos del último día con su información relevante ("¿por qué se mueve?"); índice del universo; series macro de 10 años; próximos 45 días del calendario |
| Screener | Los 8 screeners con lo que entró y salió frente a la corrida anterior, filtros por sector, universo y puntaje, tabla ordenable y exportación a CSV |
| Matriz | Fundamental × técnico de 1 a 10 (informativa mientras no esté validada), coloreada por el tercio de los factores validados, con la evidencia de cada factor y enlace al tablero de factores validados |
| Valoración | P/VL o P/U de cada acción frente a su historia (1, 3, 5 o 10 años): actual, media, desviación, z, percentil, cobertura y aviso de quiebre, con filtro por sector y universo líquido, exportación a CSV y, al hacer clic, la gráfica de bandas. La Ficha trae el mismo panel ("Valoración frente a su historia") |
| Ficha | Resumen (precio, puntaje global, cuadrante, avisos), puntaje por factor, técnico, precio ajustado con su media de 200 (6M a 5A), métricas del puntaje con su z y pares, riesgo a 1, 3 y 5 años en pesos y dólares, sección Banco si el emisor tiene cifras del CUIF (rentabilidad, riesgo de crédito, balance, 5 años y los bancos pares), información relevante, dividendos y comparación con su sector |
| Graficador | Hasta 6 acciones y los índices en una misma gráfica (ver abajo) |
| Mi cartera | Valor, ganancia no realizada, TWR, XIRR, dividendos por cobrar; mapa de posiciones, desvío frente a las bandas del perfil, próximos dividendos, valor diario y posiciones |
| Carteras y transacciones, Perfiles, Tesis | Formularios validados (perfiles con versiones y asignación objetivo) |
| Salud de datos | Estado de las fuentes, últimas corridas, validación de factores, tamaño de las tablas y botones para leer la Bolsa de Colombia (abre la ventana de Chromium), actualizar el modelo y verificar el contrato |
| Aprender | La explicación breve de cada variable; el ícono ⓘ de cada página abre la misma explicación ("Modo aprendizaje" lo muestra u oculta) |

Rutas: `#/mercado`, `#/screener`, `#/matriz`, `#/valoracion/<especie>` (la especie es opcional), `#/ficha/<especie>`, `#/graficador?series=…&metrica=…&periodo=…`, `#/cartera/<cartera_id>` (sin id, la última elegida), `#/carteras`, `#/perfiles`, `#/tesis`, `#/salud`, `#/aprender/<variable>`.

**Graficador (`#/graficador`).** Compara hasta 6 acciones (buscador con nemotécnico o nombre) y, en las métricas en base 100, los índices del universo líquido (pesos iguales y por capitalización, `fact_indice_mercado`) y el MSCI COLCAP (`fact_macro`, mientras acumula historia). Métricas: precio, retorno total y retorno total en dólares en base 100; P/VL y P/U (de `fact_valoracion_diaria` cuando exista; si no, a fin de mes de `fact_valoracion`); rendimiento del dividendo de 12 meses; volatilidad de 60 días (`fact_tecnico_diario`) y puntaje global (`fact_matriz`). Periodos 6M, 1A, 3A, 5A, 10A y Máx.; escala logarítmica en base 100 y en múltiplos. Las series se alinean por fecha sin rellenar huecos (el tooltip dice "sin dato"; la línea se corta si pasan más de 45 días, o 100 en series mensuales) y la base 100 es el primer día con dato de todas las series: si una empieza tarde (Cibest, mayo de 2025), la gráfica lo marca y antes de esa fecha cada línea muestra su valor frente a la base. Un solo eje, colores fijos por acción (paleta validada para el fondo oscuro; los índices van en gris con trazo propio), leyenda, rótulo al final de cada línea con 4 series o menos, crosshair con el valor de todas las series (también con las flechas del teclado) y tabla con valor inicial, final, variación, máximo y mínimo, exportable a CSV (el resumen o las series alineadas). La URL guarda la selección (`#/graficador?series=ECOPETROL,CIBEST,ISA,GEB,universo_igual&metrica=retorno_total&periodo=5a`; un hueco `,,` conserva el color de las demás al quitar una) y la ficha de cada acción abre el graficador con sus pares del sector ("Comparar en el graficador"). API: `/api/graficador?series=&metrica=&periodo=` (lista cerrada de métricas y periodos, máximo 6 acciones, acepta código SFC, nemotécnico o nombre corto) y `/api/graficador/opciones`.

**Prueba de las páginas (`scripts/probar_paginas.py`).** Arranca el servidor en un puerto libre con la carpeta de datos indicada, abre cada página en Chromium sin ventana (las del menú, tres fichas, Valoración con una especie elegida, el graficador con esas acciones en base 100 y en P/VL, cada cartera por su id, una variable de Aprender y una especie inexistente, que debe mostrar el error 404) y la marca como fallida si hay errores en la consola o excepciones de JavaScript, pedidos a `/api/*` con código >= 400 (salvo los intencionales), redirecciones o `undefined`/`null` en la ruta, o si la página queda vacía, en "Cargando…", con el panel de error de la app o con `undefined`/`NaN` en el texto. Imprime una línea por página y devuelve 0 si todas pasan:

```
uv run --extra app --extra pc python scripts/probar_paginas.py --datos ..\invest-data
uv run --extra app --extra pc python scripts/probar_paginas.py --datos ..\invest-data --paginas mercado ficha/coc04paao008 --capturas %TEMP%\capturas
```

`--python` elige el intérprete del servidor (por omisión la `.venv` del repositorio) y `--capturas` guarda un PNG por página. Solo lee los datos (si falta `modelo.duckdb`, la construye como `invest app`). `tests/test_paginas_app.py` hace lo mismo dentro de `pytest` con un conjunto de datos sintético (`tests/app_datos_util.py`: seis acciones, tres años de precios, el modelo completo y dos carteras), con una carpeta casi vacía y, si existe, con una copia de la carpeta de demostración (`INVEST_DEMO_DIR`, por omisión `/tmp/demo`); se salta si no están Playwright y Chromium (extra `pc` y `playwright install chromium`).

## Agregar una fuente

1. Registrarla en `config/catalogo_fuentes.yaml` (acceso, frecuencia, `recarga_dias`, `ejecuta_en`, licencia, `redistribuible`).
2. Crear `src/invest/ingest/<fuente>.py` con una clase que herede de `Conector` e implemente `descargar`, `interpretar` y `validar`.
3. Registrarla en `src/invest/ingest/__init__.py` y cambiar su `estado` a `implementado`.
4. Agregar pruebas con una muestra real en `tests/fixtures/`.

## Repositorios y automatización

Ver [docs/configurar_github.md](docs/configurar_github.md) para crear los dos repositorios, el token y los workflows, y [docs/programador_tareas.md](docs/programador_tareas.md) para las tareas que corren en el PC.

Reglas que no se rompen:

- Ningún dato va al repo público; los registros de ejecución de un repo público los ve cualquiera.
- Los datos con licencia restringida (`redistribuible: false`) solo van a `invest-data`.
- No se burlan captchas ni bloqueos de ningún sitio.
