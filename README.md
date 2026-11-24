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
| 1 · Bolsa de Colombia y BanRep | Cierre, máximo y mínimo diarios de la BVC (`fact_precio_bvc`), MSCI COLCAP y MSCI NUAM USD, tasa de política monetaria e IBR en `fact_serie_macro`; con Playwright en el PC | Hecho (BanRep automático; BVC de lectura manual, primera lectura 23-nov-2026) |
| 1 · Resto del MVP | NIIF de la SFC para el resto de emisores, perfiles y cartera, plantillas y puntaje con validación estadística | Pendiente |

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

## Solo lo nuevo en cada corrida

Después de la primera carga, ninguna corrida vuelve a bajar lo que ya tiene:

| Fuente | Cómo evita repetir |
| --- | --- |
| TRM | Pide a la API solo desde el último dato guardado menos 10 días (correcciones) |
| Índice SFC | Descarga condicional: si la página no cambió, el servidor responde "sin cambios" (304) sin contenido |
| Ficha y precios SFC | Si la SFC no publicó un reporte nuevo, no se pide nada. Si lo publicó: fichas con descarga condicional y el .xls de precios solo de las acciones cuya ficha muestra una operación posterior a la última guardada |
| Información relevante | Consulta solo desde la última fecha guardada menos 3 días; los PDF ya guardados no se vuelven a bajar |
| Dividendos (dos agentes) | Solo los PDF nuevos o que cambiaron; lo ya procesado no se vuelve a enviar |

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
- **Carga del 23-nov-2026:** 2.220 pagos de 22 acciones (Terpel, Grupo Bolívar y Banco de Occidente se sumaron ese día); 2.180 verificados solos; 28 decididos con fuentes (notas en el CSV): se rechazan las escisiones (se ajustan aparte, ver abajo); los dividendos de Aval y Banco de Bogotá de 2022 y el extraordinario de Grupo Bolívar de 2024 se aprueban como efectivo, porque las acciones solo se entregaban a quien las pedía; el de 593 de Ecopetrol en 2023 se carga en sus tres cuotas. 15 quedan fuera por las exclusiones configuradas (GEB antes de 2012, Promigas antes de oct-2014, Banco de Bogotá dic-2010 a ago-2011). Pendientes: Corficolombiana (Investing corta su historial en 2023 y mezcla dividendos en acciones; se toma de los PDF de la SFC), Conconcreto (no está en Investing) y las poco líquidas.
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
| `fred` | `ingest/fred.py` | `fact_serie_macro` (`fred_<id>`) | CSV público sin clave; series en el catálogo. La serie del IPC de Colombia en FRED se cortó en 2023 |
| `cuif_datos_gov` | `ingest/cuif.py` | `fact_cuif_mensual` | Primer nivel de cuentas, moneda total, 8 bancos inscritos; control activo = pasivo + patrimonio. La API tarda unos 40 s por consulta |
| `banrep_tasas` | `ingest/banrep.py` | `fact_serie_macro` (`banrep_<id>`) | Páginas públicas de SUAMECA leídas con Playwright: se guardan las respuestas que la propia página recibe (`consultaMenuXId` y `consultaInformacionSerie`). TPM e IBR en todos sus plazos |
| `bvc_indices` | `ingest/bvc.py` | `fact_serie_macro` (`msci_colcap`, `msci_nuam_usd`) | Valor de hoy y de la sesión anterior, leídos de la página renderizada de la BVC |
| `bvc_web` | `ingest/bvc.py` | `fact_precio_bvc` | "Mercado local en línea": cierre, máximo, mínimo, volumen. Solo guarda si son más de las 4:15 p. m., es día hábil, la página trae la fecha de hoy y no dice "abierto". Nemotécnicos → especie SFC en `config/nemotecnicos.yaml`. No hay histórico: se acumula desde la primera corrida |

**Lectura manual de la Bolsa de Colombia.** La BVC (su CDN, CloudFront) responde 403 "Request blocked" a los navegadores sin ventana. No se esquiva: `bvc_web` y `bvc_indices` tienen `ejecuta_en: manual`, no entran en `--todas-pc` ni en tareas programadas, y sin consola (una tarea programada) se niegan a abrir la página. Se lanzan a mano y abren una ventana normal de Chromium, como si se abriera la página. Los días sin lectura quedan sin cierre/máx./mín. (el precio promedio sigue llegando de la SFC) y la salud los muestra como "manual, sin leer reciente" sin marcar falla. Si la tabla no aparece, queda captura y texto en `invest-data/diagnostico/`.

**Quién escribe qué en `invest-data`.** GitHub Actions (cada noche, 19:17) y la tarea del PC (19:47) suben al mismo repositorio, así que nunca escriben el mismo archivo: las series macro van en una carpeta por fuente (`silver/fact_serie_macro/fuente=<id>/anio=<año>`, y `leer_tabla` las une); gold (`invest dims`, `invest factores`) solo se construye en el PC; la bitácora `meta/*.jsonl` se fusiona sumando líneas (`.gitattributes`, `merge=union`). La tarea del PC guarda sus cambios locales en un commit antes del `git pull --rebase`, y los dos lados reintentan el push si el otro subió algo entre tanto.
| `sec_edgar` | `ingest/sec_edgar.py` | `fact_xbrl_sec` | `companyfacts` de la SEC (NIIF, en pesos): todos los conceptos `ifrs-full` con formulario, radicación y fecha de presentación (para evitar sesgo de anticipación). Requiere `SEC_USER_AGENT` = "Nombre correo@dominio" |

### Estados financieros de los emisores (`sfc_rnve_niif`)

`ingest/sfc_niif.py` lee la página "Informes financieros bajo NIIF y anexos" del SIMEV de la SFC para cada emisor de `config/emisores_rnve.yaml` (36 emisores con sus códigos SIMEV; algunos tienen varios porque la SFC los reclasificó). La página pide los datos con una clave propia del sitio que no se replica: Playwright elige año, tipo de informe y periodo como una persona y guarda las respuestas que la página recibe.

| Tabla silver | Qué trae | Cobertura |
| --- | --- | --- |
| `fact_anexo_sfc` | Anexos por celda (formato, fila, columna, valor): 141 información económica general (valor patrimonial y utilidad por acción, dividendos decretados, ventas, empleados), 211 acciones en circulación y accionistas, 529 principales accionistas, 058 subordinadas | Todos los emisores, trimestral |
| `fact_xbrl_sfc` | Hechos XBRL NIIF sin dimensiones (balance, resultados, flujos, por acción), del periodo y del comparativo, con corte y fecha de transmisión | Solo los emisores para los que el SIMEV publica el XBRL (Ecopetrol sí; Celsia, Cementos Argos y Grupo Argos no, nov-2026) |

Montos en miles de pesos; cifras por acción en pesos (columna `escala`). La carga inicial (2015 en adelante, unas 17 horas de navegador) va en tandas de 80 cortes por corrida (una hora): primero el último año, luego los cierres anuales hacia atrás y al final los demás trimestres; muestra el avance por emisor. Si la lista de informes trae XBRL pero la descarga falla, el corte se reintenta hasta tres veces en corridas siguientes. Canacol reporta en dólares (escala `miles_de_dolares`). Prueba de un emisor sin guardar: `invest probar-niif 260 36 2025-09-30`.

Primera carga (sep-2025, 23-nov-2026): 32 de 36 emisores con anexos y 8 con XBRL (Canacol, GEB, ISA, Promigas, ETB, Grupo Sura, Grupo Bolívar, Grupo Aval); sin nada Banco Popular, BBVA, Bolsa Mercantil y Davivienda Group. Revisión de orden de magnitud: activos consolidados de ISA 77,5 billones y de Grupo Aval 343,8 billones (miles de pesos × 1.000); falta cotejar cifra por cifra con los estados publicados. El XBRL de cada informe pesa unos 5 MB y no va al repositorio.

El reporte de salud mide el rezago según la frecuencia de cada fuente (diaria 3 días hábiles, mensual 50, anual 360).

## Factores y validación (`src/invest/factores.py`, `invest factores`)

Cada fin de mes (última sesión XBOG), con lo que se sabía a esa fecha: universo de acciones líquidas con al menos 250 días de negocios y un negocio en las últimas 10 sesiones; factores de `config/factores.yaml` sobre un índice de retorno total que no cuenta los saltos atípicos. Salidas: `gold/fact_factor_mensual` y `gold/fact_validacion_factor`.

Validación por factor, con el signo de su hipótesis: IC de Spearman mensual contra el retorno del mes siguiente, t de Newey-West, corrección de Holm por probar varios factores, spread entre el tercio superior y el inferior, y muestra de descubrimiento (2008-2017) contra fuera de muestra (2018 en adelante). Solo un factor `validado` puede alimentar la matriz de decisión.

Primera corrida (23-nov-2026, 223 meses, mediana de 16 acciones por mes): **validados** momentum 12-1 (IC 0,061, t 3,2, positivo en 15 de 19 años) y rendimiento por dividendo de 12 meses (IC 0,064, t 3,8, positivo en 14 de 19 años); se sostienen con 6 rezagos de Newey-West, desde 2010 y con un mínimo de 12 acciones por mes. No validados: momentum 6-1, reversión de un mes, baja volatilidad y liquidez. El spread de tercios es de unos 0,6 % mensual (Sharpe anual cercano a 0,4) antes de costos de transacción. **Con costos** (0,5 % por compra o venta, `costo_ida` en `config/factores.yaml`) y solo comprando, que es lo realista en la BVC: el tercio superior le gana al promedio del universo por 0,4 % mensual bruto y 0,1 % neto en momentum (rota 27 % al mes) y 0,2 % bruto y casi 0 neto en dividendo (rota 18 %). Los dos factores casi no se solapan (correlación media 0,15); su combinación tiene IC 0,077 (t 4,2) y 0,2 % mensual neto, pero es informativa porque se armó después de ver los resultados. La correlación entre factores queda en `gold/fact_correlacion_factor`. Conclusión: los factores ordenan, pero la ventaja que queda después de costos es pequeña.

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
