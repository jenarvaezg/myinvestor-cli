# Changelog

Los cambios de cada versión de `myinvestor`. El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y las versiones, [SemVer](https://semver.org/lang/es/) (mientras sea 0.x, una versión menor puede romper la compatibilidad, y se avisa aquí).

**Salida JSON.** Cada orden con `-f json` tiene un esquema con versión, `myinvestor.<orden>/<n>` (ver [docs/json.md](docs/json.md)). Quitar o renombrar una clave, cambiar su tipo o hacer que admita `null` es un cambio incompatible: **sube el número de esquema** de esa orden (`myinvestor.movements/1` → `/2`) y se anota en la sección «Esquemas JSON» de la versión. Añadir una clave es compatible: no cambia el número, pero también se anota.

## [0.2.0] - sin publicar

### Esquemas JSON

- Primera versión del contrato: todas las órdenes con `-f json` pasan a `myinvestor.<orden>/1` con las claves y tipos de [docs/json.md](docs/json.md); `raw` y `fund` (respuesta de la API tal cual) van con `myinvestor.raw/1`, que solo garantiza el envoltorio, y la línea de error de stderr es `myinvestor.error/1`. `snapshot` lleva su propio `schema` (`myinvestor.snapshot/1`) y nunca va en el envoltorio. En 0.1.0 no había contrato.
- Nuevos `myinvestor.transfers/1` y `myinvestor.interest/1`. `myinvestor.movements/1` gana `beneficiario` e `iban_destino`, solo con `--details` (compatible). En `myinvestor.upcoming/1`, `tipo` puede valer `transferencia` (#4, #5).
- Nuevos: `myinvestor.order/1` (objeto `{orden, ejecuciones}`), `myinvestor.lots/1` y `myinvestor.gains/1` (#3, #1).
- Nuevo `myinvestor.fund-transfers/1`, con la variante `--pending` (#8).
- `myinvestor.accounts/1` gana `tipo_pct` y `tae_pct` (números en %, `null` si la cuenta no tiene remuneración; `tae_pct` también es `null` si MyInvestor manda 0, «sin calcular») (compatible) (#5).
- Nuevos `myinvestor.fund-filters/1` y `myinvestor.pension-plans/1`; `myinvestor.funds/1` no cambia con los filtros (#6).
- Nuevo `myinvestor.fund-history/1`: filas `{fecha, vl, moneda}`. `myinvestor.funds/1` gana `moneda` (divisa del fondo; compatible) (#7).
- `myinvestor.upcoming/1` y la sección `upcoming` de `myinvestor.snapshot/1` ganan `estado` (`programada` o `en_curso`) (compatible: ningún campo cambia de significado). En `myinvestor.holds/1` y `pending.holds` de `snapshot`, `importe` es siempre negativo, que es lo que ya mandaba la API (#58, #57).
- Nuevo `myinvestor.portfolios/1` (catálogo; con CARTERA, objeto `{cartera, fondos}`; con `--mine`, filas con `posiciones` y `rentabilidad_anual`). `myinvestor.position/1` gana `cartera`, `perfil`, `efectivo` y `efectivo_id`, y `myinvestor.holdings/1`, `cartera` y `perfil` (compatible: claves añadidas, `null` fuera de las carteras del roboadvisor). En `position`, el `valor` de una cartera incluye el saldo de su cuenta de efectivo propia, que deja de salir como fila `efectivo` aparte (#6).

### Añadido

- `--json-envelope` (o `MYINVESTOR_JSON_ENVELOPE=1`): `{"schema", "cli_version", "as_of", "data"}`; sin la opción la salida no cambia. `myinvestor schema [ORDEN] [--envelope]` imprime el JSON Schema de cada salida, generado de la misma tabla que los tests de contrato (#30).
- `movements`: id estable de cada movimiento, `--id`, `--owner` y los campos `cuenta_id`, `cuenta_uuid`, `iban`, `sentido`, `clase`, `origen`, `destino`, `contraparte`, `operacion_ref` y `fichero_ref`; `accounts`: `tipo`, `rol`, `titular` y, en JSON, `titulares` (sin DNI) (#35).
- Formato `-f tsv` y `--wide` (#36).
- Códigos de salida por tipo de error (2 uso, 10 credenciales, 11 sin sesión, 12 red/WAF, 13 segundo factor o resultado parcial, 14 almacén corrupto) y, con `-f json`, el error como una línea JSON en stderr (#38).
- `-q`/`--quiet`, `-v`/`--verbose` (una línea por petición, sin secretos), `--min-interval` y `--retry-blocked` (#37).
- `movements` más allá de ~90 días: tramos con una firma por cuenta, `--free-only`, `--allow-partial` y `--no-signature` (#40).
- `status -f json` y `--offline`, `doctor` y `--version` (#41).
- `position`: `fecha_valor`, `consultado_en` y fila `total` en JSON/CSV/TSV; `holdings`: `consultado_en`; orden `income` (intereses, dividendos, retenciones y comisiones) (#42).
- Código SMS del `login --direct` sin terminal: `MYINVESTOR_OTP`, `--code-stdin`/`--code-fd`, `/dev/tty` y login en dos pasos con `--start`/`--code` (#43).
- `internal-transfers`, y `pareja_id`/`traspaso_interno` en `movements` (#39).
- `upcoming`: aportaciones, órdenes en curso y retenciones contra el disponible (#44).
- `allocation` (reparto, TER medio y `--target`), y `peso`/`--ter` en `holdings` (#45).
- `--new`, `--since-date`, `--since-id` y `state show|mark|reset`; `export` incremental con `manifest.json` (#47).
- `snapshot` (un JSON diario, esquema propio `myinvestor.snapshot/1`, con `delta` aportado/mercado frente a la foto anterior, `--save DIR`, `upcoming` y errores por sección) y `summary` (la misma foto en tablas, o una guardada con `--from-file`) (#25, #33).
- `documents` (las cinco listas de documentos), `documents get` (descarga del PDF por id, nombre o `--fiscal AÑO`, 0600, sin sobrescribir), `documents summary` (extracto de una cuenta) y `xray` (radiografías); esquema `myinvestor.documents/1` (#2).
- `transfers` (transferencias emitidas y programadas: beneficiario, IBAN de destino, estado, periodicidad y próxima fecha) e `interest` (última liquidación de intereses: tipo, TAE, brutos, retención y neto); `upcoming` (y la sección `upcoming` de `snapshot`, con una petición más por cuenta de efectivo) incluye las transferencias programadas (`tipo: transferencia`) y `movements --details` añade `beneficiario` e `iban_destino` a los cargos de transferencias emitidas (#4, #5).
- `order REF`: una orden con su ejecución (VL, participaciones, comisiones, retenciones, neto y resultado fiscal) (#3); `lots`: lotes vivos FIFO de cada fondo con su plusvalía latente, y `gains [--year]`: plusvalías y minusvalías realizadas en reembolsos y traspasos de salida (#1).
- `fund-transfers`: traspasos de fondos de las cuentas de valores o, con `--pension`, de planes de pensiones (por defecto, los últimos 60 días, como la web; `--since`/`--until`), y `--pending` con el contador de traspasos en curso. Los cambios entre fondos dentro de MyInvestor siguen en `orders`/`lots` (#8).
- `accounts`: columnas `tipo_pct` y `tae_pct` de la última liquidación, como en `interest` (una petición más por cuenta; vacías si la cuenta no tiene remuneración o la consulta falla, sin abortar la orden; una TAE de 0 sale vacía) (#5).
- README: hallazgos del servicio `/read-only/api/v1` (copia diaria, no sirve como fuente principal ni para más de 90 días) (#10).
- Filtros del buscador en `funds` (`--category`, `--manager`, `--currency`, `--asset-type`, `--region`, `--min-ter`/`--max-ter`, `--risk N[-M]`, `--rating`, `--esg`, `--hedged`), `fund-filters [FILTRO] [--pension]` (valores válidos de cada filtro) y `pension-plans [TEXTO]` (catálogo de planes de pensiones con TER y rentabilidades, con filtros) (#6).
- `fund-history ISIN [--since] [--until] [--every day|week|month]`: valor liquidativo de cualquier fondo del catálogo desde su inicio (`…/funds/{isin}/graphs/INIT_GRAPH`), con la variación frente al primer punto en la tabla; README: `INIT_GRAPH` es el único `graphType` válido y no hay más de un año de historial de las posiciones propias. `funds` añade `moneda`, y en las tablas los importes y precios de una fila con `moneda` salen en esa moneda (antes, siempre «€»: un fondo en USD salía en euros) (#7).
- `upcoming`: `estado` en cada fila (`programada`: aportaciones y transferencias; `en_curso`: órdenes y retenciones, aunque su fecha haya pasado) y `--future-only`, que deja solo lo programado; la regla para no contar dos veces lo de `pending` en `snapshot` está en docs/snapshot.md (#58).
- `portfolios [CARTERA] [--mine]`: catálogo de carteras del roboadvisor (perfil, % RF/RV, rentabilidad, horizonte), composición objetivo de una y, con `--mine`, desviación de tus carteras frente a los pesos objetivo y su rentabilidad anual. `position` muestra una cartera contratada como «Cartera METAL (muy agresiva)» con su cuenta de efectivo propia dentro (ya no sale suelta) y `holdings` añade `cartera` y `perfil` (#6).

### Cambiado

- **Incompatible:** en JSON los importes, precios y porcentajes son números (antes, texto), con el valor exacto del `Decimal`; los importes van redondeados a céntimos en JSON/CSV (#36).
- **Incompatible:** la fila del total de `position` se llama `total` en JSON/CSV/TSV (antes `TOTAL`) (#42).
- **Incompatible:** el código de salida depende del tipo de error (antes, 1 casi siempre) (#38).
- El re-login desatendido solo borra la contraseña guardada si MyInvestor dice que es incorrecta; con cualquier otro rechazo se pausa 24 h (#38).

### Corregido

- Una respuesta de MyInvestor con un formato inesperado ya no acaba en un traceback: sale un error claro con el modelo y las claves afectadas (sin sus valores), código 1 y, con `-f json`, `"error": "unexpected_response"` en stderr.
- `--approval-timeout` también vale en el login y el re-login; `logout` borra la pausa del re-login (#46).
- `movements --id -f json` redondea los importes a céntimos, como la lista (antes, `-50.0` en vez de `-50.00`) (#30).
- El error de `--account` sin coincidencias dice «no coincide con ninguna cuenta» (antes, «coincide con ninguna cuentas»); con varias, «coincide con varias cuentas».
- Las retenciones (`holds`, `pending.holds` de `snapshot`) salen siempre en negativo, como un cargo en `movements`, aunque una respuesta las traiga en positivo. Se ha corregido el ejemplo de docs/snapshot.md, que ahora se valida en los tests contra el contrato (#57).
- README: en las transferencias recibidas de otros bancos la API no da el ordenante; `origen`, `destino` y `contraparte` van a `null` (#56).
- Los tests ya no dependen de `FORCE_COLOR`, `NO_COLOR`, `TERM`, `COLUMNS` ni `CI` del entorno, y no pueden leer el llavero ni el fichero de sesión reales: `doctor` también usa el almacén del test (#53, #54).
- Las tablas ya no parten palabras por la mitad (`Carter`/`a Metal`, `APERTU`/`RA` en 80 columnas) y nunca pasan de `COLUMNS` salvo que ni las fechas y los importes quepan: primero ceden ancho los textos largos (concepto, nombre, detalle…) y después las etiquetas (cuenta, tipo, estado, ISIN, ref…), que saltan de línea en los espacios y, si una palabra no cabe, la acortan con `…`. En `movements -f table`, `valor_fecha` se oculta cuando coincide con `fecha` en todas las filas y la tabla no cabe (#55).

## [0.1.0]

Primera versión.

- Login con navegador real (Playwright), directo con DNI y contraseña, o importando un refresh token de la web; renovación automática de la sesión y re-login desatendido con `--remember`.
- Segundo factor: aprobación en la app del móvil y código SMS; `--approve` para esperar sin terminal.
- Órdenes de lectura: `position`, `accounts`, `movements`, `holds`, `balance-history`, `holdings`, `orders`, `pensions`, `history`, `contributions`, `fund`, `funds`, `feed` y `raw`.
- Solo lectura forzado en el código: solo se envían GET y las llamadas de firma.
- Importes en `Decimal`, reintentos en peticiones idempotentes y salida `table`, `json` y `csv` (en JSON, los números como texto).
