# myinvestor-cli

Cliente Python y CLI **no oficiales y de solo lectura** para [MyInvestor](https://myinvestor.es), construidos observando las llamadas de su web app (`newapp.myinvestor.es`, versión 3.134.0). No hay API pública: esto puede romperse cuando MyInvestor cambie su backend. El CLI no compra, vende, traspasa ni mueve dinero: no implementa ninguna escritura.

## Instalación

```bash
uv tool install 'myinvestor[browser] @ git+https://github.com/jenarvaezg/myinvestor-cli'
# o, desde el repo:
uv sync --extra browser && uv run myinvestor --help
myinvestor --version
```

El extra `browser` (Playwright) solo hace falta para `myinvestor login` sin flags. Usa el Chrome instalado; si no hay, `uv run playwright install chromium`.

## Login

La web manda un token de **reCAPTCHA Enterprise** en el login, pero el servidor también acepta el login sin él desde un dispositivo ya conocido (verificado el 2026-11-03). Hay tres formas de entrar:

```bash
myinvestor login                                  # abre un navegador: entras como siempre (usuario, contraseña, SMS)
myinvestor login --direct -u 12345678Z            # por terminal, sin reCAPTCHA (ver «Estado de --direct»)
echo "$PASS" | myinvestor login --direct -u 12345678Z --password-stdin   # también vale MYINVESTOR_PASSWORD
myinvestor login --direct --remember -u 12345678Z # guarda la contraseña → re-login automático (cron, con --store file)
myinvestor login --direct --start -u 12345678Z    # SMS en dos pasos (sin terminal): envía el código y sale con 13…
myinvestor login --direct --code 123456           # …y esto lo completa (ver «Código SMS sin terminal»)
myinvestor login --refresh-token-stdin --device-id <_capuid> < token.txt  # importar una sesión de la web
myinvestor status [-f json] [--offline]           # ¿sesión viva? (sin --offline la renueva)
myinvestor doctor [-f json] [--offline]           # diagnóstico completo, ver «Diagnóstico»
myinvestor logout [--forget-device]
```

- El **access token dura 15 min y el refresh token 30 min**. Cada petición renueva el par si hace falta, así que la sesión vive mientras la uses al menos cada media hora; si no, hay que repetir el login.
- Con `--remember` (solo con `--direct`), la contraseña se guarda en el llavero del sistema (o, con `--store file`, en claro en `~/.config/myinvestor/session.json`, 0600: ver «Qué guarda cada almacén») y, cuando el refresh token ha caducado, el CLI vuelve a entrar solo, como la app de Fintonic con el PIN. Se guarda solo si MyInvestor la acepta y se borra con `logout`. Si el re-login automático falla, depende del `errCode` del rechazo: con `UNAUTHORIZED` (contraseña incorrecta) o `UPDATE_PASSWORD_REQUIRED` **se borra en el acto**, para no bloquear la cuenta repitiéndola en cada ejecución; con cualquier otro código, o sin él (reCAPTCHA obligatorio, demasiados intentos, cambios de la API…), **no se borra**, pero el re-login queda en pausa 24 h (o hasta el siguiente `myinvestor login`), así que se intenta como mucho una vez al día. Si lo que falta es un código SMS, falla (código 13) sin tocar la contraseña y **sin mandar el SMS**, salvo que hayas definido `MYINVESTOR_OTP` (ver «Código SMS sin terminal»). Sin `--remember` la contraseña no se guarda nunca: en el almacén solo van los tokens y el id de dispositivo.
- El navegador usa un perfil propio (`~/.local/share/myinvestor/browser`) para que MyInvestor reconozca el dispositivo y solo pida el SMS la primera vez. Al terminar se borran los tokens de ese perfil: la sesión pasa a ser del CLI.
- `--refresh-token-stdin`: el token está en el localStorage de la web (`myinvestor-app:REFRESH_TOKEN`) y el id de dispositivo en `_capuid`. Cuidado: es una credencial.

### Estado de `--direct`

A fecha de 2026-11-03, `login --direct` **funciona** sin reCAPTCHA desde un dispositivo que MyInvestor ya conoce, y es la vía recomendada para uso desatendido: `--direct --remember` con `--store file` (guía: [docs/cron.md](docs/cron.md)). Depende de que MyInvestor siga sin exigir el token de reCAPTCHA. Si algún día lo exige, el login falla con «MyInvestor ha rechazado el login (errCode …, HTTP …). No parece una contraseña incorrecta…» (código 1); en el re-login automático la contraseña guardada **no** se borra y el re-login queda en pausa 24 h (código 11). Qué hacer entonces:

1. `myinvestor doctor`: ¿MyInvestor está en mantenimiento?, ¿ha cambiado la versión de la web?, ¿bloquea Imperva tu IP?
2. Esperar un rato y repetir `myinvestor login --direct --remember`, por si es un bloqueo temporal (demasiados intentos).
3. Si sigue fallando, entrar con el navegador (`myinvestor login`) o importar una sesión de la web (`--refresh-token-stdin`). Esas sesiones no tienen re-login automático: viven mientras se usen al menos cada 30 min.
4. Abrir un issue con el `errCode` y el HTTP del mensaje (nunca con la contraseña ni los tokens).

Segundo factor en el login `--direct`: un **código SMS** se da en la terminal o, sin ella, como en [Código SMS sin terminal](#código-sms-sin-terminal); una **aprobación en la app** (INAPP), en una terminal o con `--approve`. `--approve` no sirve para el SMS. Dato observado el 2026-11-03, no una garantía: el primer login desde una IP de datacenter (un dispositivo nuevo) no pidió segundo factor.

### Qué guarda cada almacén

`--store` (o `MYINVESTOR_STORE`) elige dónde: `keyring` (el llavero del sistema), `file` o `auto` (por defecto: `keyring` si hay un llavero usable y si no `file`). El login y las ejecuciones posteriores tienen que usar **el mismo**: en cron o systemd el llavero suele no estar disponible o estar bloqueado, así que `auto` podría elegir distinto en tu terminal y en el cron; fija `MYINVESTOR_STORE=file` en los dos.

| Dato | Cuándo se guarda y cuándo se borra | `keyring` (servicio `myinvestor-cli`) | `file` (clave del JSON) |
|---|---|---|---|
| Sesión: id de dispositivo, DNI/NIE, access y refresh token | en cada login y en cada renovación; `logout` borra los tokens y conserva el id de dispositivo (para que el próximo login no pida segundo factor); `logout --forget-device` la borra entera | `session` | `session` |
| Contraseña | solo con `--direct --remember`, y solo si MyInvestor la acepta; se borra con `logout`, al entrar con otro usuario y si el re-login recibe `UNAUTHORIZED` o `UPDATE_PASSWORD_REQUIRED` | `password:<DNI>` | `passwords.<DNI>`, **en claro** |
| Pausa del re-login | cuando MyInvestor rechaza el re-login por otro motivo: hasta cuándo (24 h) y el error; se quita con el siguiente login correcto (manual o re-login tras la pausa) y con `logout` | `relogin-block:<DNI>` | `relogin_blocks.<DNI>` |
| Login pendiente de `--start` | `signatureRequestId`, id del código enviado, método, destino enmascarado, DNI, id de dispositivo, hora e intentos fallidos; la contraseña solo con `--start --remember`; se borra al completarlo con `--code`, al agotar los intentos o si la firma caduca o se rechaza, con cualquier login correcto y con `logout` | `pending-login` | `pending_login` |

Protección:

- **`keyring`**: lo cifra y lo desbloquea el llavero del sistema (Keychain en macOS, Secret Service en Linux, Credential Locker en Windows). No hay fichero ni bloqueo entre procesos.
- **`file`**: `$XDG_CONFIG_HOME/myinvestor/session.json` (por defecto `~/.config/myinvestor/session.json`), o la ruta de `--store-path`/`MYINVESTOR_STORE_PATH`. El directorio se crea con permisos 0700 y el fichero se escribe 0600 (en un temporal que se renombra, así que nunca queda a medias); al lado, `session.lock` (0600) serializa las escrituras entre procesos con `flock`. Es JSON **sin cifrar**: quien pueda leerlo (tu usuario, root, una copia de seguridad) tiene la contraseña recordada y los tokens. `myinvestor doctor` comprueba los permisos.

## Uso

```bash
myinvestor position                               # efectivo + cuentas de valores + planes, con total y fecha de valor
myinvestor accounts [--owner me|junior|other]     # cuentas de efectivo: tipo, tu rol, titular, disponible, retenido, saldo
myinvestor movements [-a "Mi cuenta"] [--from 2026-10-01 --to 2026-10-31 | --days 60] [-s nómina] [--oldest-first] [--owner me|junior|other] [--free-only] [--allow-partial]
myinvestor movements --id <id> [--from … | --days …]   # un solo movimiento, con todos sus campos
myinvestor movements --new [--no-mark] [--state PATH] | --since-date 2026-11-01 | --since-id <id>   # solo lo nuevo
myinvestor internal-transfers [--from … --to … | --days …] [-a Junior] [--unmatched] [--free-only | --allow-partial]   # traspasos entre tus cuentas: cargo ↔ abono
myinvestor holds                                  # saldo retenido (p. ej. por órdenes en curso)
myinvestor balance-history [--from 2026-01-01]    # saldo diario total del efectivo
myinvestor income [--year 2026 | --from … --to … | --days 60] [--by month|year|type|account] [-a …] [--free-only] [--allow-partial]   # intereses, dividendos, retenciones y comisiones

myinvestor holdings [-a 123456] [--kind fondos|broker] [--ter]   # posiciones: participaciones, coste, VL, valor, rentabilidad y peso
myinvestor allocation [--by asset|geo|category|product|account] [--include-cash] [--target objetivo.json]   # reparto de la cartera y TER medio
myinvestor orders [--all | --from 2025-01-01 --to …] [--status EXECUTED] [--pension] [--new]
myinvestor pensions                               # planes de pensiones
myinvestor history [IE0032126645|N1234] [-a ID] [--period month|three_months|six_months|year]
myinvestor contributions [--all] [--new]          # aportaciones periódicas
myinvestor upcoming [--days 14] [--owner me|junior|other]   # lo que viene: aportaciones, órdenes en curso, retenciones y si el disponible lo cubre
myinvestor snapshot [--save DIR] [--previous FILE] [--no-new] [--no-mark]   # todo lo anterior en un JSON, con el delta (aportado/mercado)
myinvestor summary [--from-file FILE] [--previous FILE]   # la misma foto, en tablas
myinvestor fund IE0032126645 [--full]             # ficha del fondo (o del plan: `fund N1234`)
myinvestor funds "s&p 500" [--sort ter,asc] [-n 20] [--page 0]
myinvestor feed [-n 20]                           # «Actividad» de la web
myinvestor raw /cperf-server/api/v2/cash-accounts/self [-p clave=valor]   # GET arbitrario

myinvestor state show|mark|reset                  # estado local de --new
myinvestor export DIR [--since 2024-01-01] [--format csv|json] [--allow-partial]   # histórico local incremental
myinvestor schema [movements] [--envelope]        # JSON Schema de la salida -f json de una orden
```

### Formatos de salida

Todas las órdenes de listado aceptan `-f table|json|csv|tsv`:

- **`table`** (por defecto): importes en formato español (`-12,50 €`). Las fechas y los importes nunca se cortan ni se parten; los textos largos (conceptos, nombres) se parten en varias líneas para caber en la terminal. Si ni así cabe, la tabla se sale del ancho antes que recortar un dato. Se respeta `COLUMNS`, y si la salida no es una terminal (cron, una tubería, el `!` de Claude Code, un agente) la tabla ocupa lo que necesite en vez de limitarse a 80 columnas. Con `--wide` (o `MYINVESTOR_WIDE=1`) cada fila va en una sola línea: `myinvestor --wide movements | less -S`.
- **`json`**: los importes son **números** JSON, no texto (`"importe": -12.50`), escritos con el valor exacto del `Decimal`, sin pasar por coma flotante. Importes y saldos (`importe`, `saldo`, `disponible`, `retenido`, `valor`, `invertido`, `ganancia`, `total`, las columnas de `income` y `disponible_tras`/`falta` de `upcoming`, `aportado`/`mercado` del `delta` de `snapshot`) van redondeados a 2 decimales; participaciones, VL, coste medio y porcentajes conservan la precisión de la API. `raw` y `fund` también devuelven números. En Python, `json.loads(texto, parse_float=Decimal)` los recupera sin pérdida.

  **Contrato:** las claves y tipos de cada orden, cuáles son estables y qué cuenta como cambio incompatible están en [docs/json.md](docs/json.md); los cambios, en el [CHANGELOG](CHANGELOG.md). Con la opción global `--json-envelope` (o `MYINVESTOR_JSON_ENVELOPE=1`) la salida va envuelta con su versión de esquema: `{"schema": "myinvestor.movements/1", "cli_version": "0.2.0", "as_of": "…", "data": [...]}`, para que un script detecte un cambio en vez de fallar en silencio. Sin la opción, la salida es la de siempre. `myinvestor schema ORDEN` da el JSON Schema.
- **`csv`**: los mismos valores sin formatear (`-12.50`).
- **`tsv`**: como CSV pero separado por tabuladores, sin comillas y una línea por fila (tabuladores y saltos de línea dentro de un valor se cambian por espacios); cómodo para logs, `cut` o `awk`.

### Movimientos: campos e id

En la tabla, `movements` muestra `fecha`, `valor_fecha`, `cuenta`, `tipo`, `concepto`, `importe`, `saldo` y `ref`. En JSON y CSV saca además:

| Campo | Origen en la API | Notas |
|---|---|---|
| `id` | — | id estable del movimiento (ver abajo) |
| `cuenta_id`, `cuenta_uuid`, `iban` | `accountId`, `accountUuid`, `iban` de la cuenta (o del extracto) | el alias (`cuenta`) lo puede cambiar el usuario: no sirve como clave |
| `sentido` | `flowType` | `IN` / `OUT` |
| `clase` | `operationClass` | |
| `origen`, `destino` | `operationOriginLabel`, `operationTargetLabel` | tal cual vienen; en un «CARGO POR TRANSFERENCIA» `destino` es el beneficiario |
| `contraparte` | — | `destino` en cargos y `origen` en abonos; `null` si viene vacío (en los SEPA entrantes muchas veces solo hay concepto) |
| `operacion_ref` | `operationRef` | campo de ancho fijo compartido por el cargo y el abono de una misma transferencia; se exporta sin interpretar (ver [Traspasos internos](#traspasos-internos)) |
| `fichero_ref` | `fileRef` | |
| `pareja_id` | — | `id` del otro lado si el movimiento es un traspaso entre tus cuentas; si no, `null` |
| `traspaso_interno` | — | `true` en los dos lados de un traspaso entre tus cuentas |

Lo que la API no trae sale como `null` (vacío en CSV); el CLI no rellena nada por su cuenta.

**`id`**: no cambia entre ejecuciones ni entre rangos de fechas, así que sirve para saber qué movimientos son nuevos desde la última vez (ver [Solo lo nuevo](#solo-lo-nuevo---new---since-date-y---since-id)) y para deduplicar el [histórico exportado](#exportar-el-histórico-export).

- Con `reference` (el caso normal): `{cuenta_uuid}:{reference}`. `reference` es un contador **por cuenta** (se repite entre cuentas), por eso va con el uuid delante.
- Sin `reference`: `{cuenta_uuid}:h` + los 16 primeros caracteres hexadecimales del sha256 de `cuenta_uuid|fecha|valor_fecha|importe|concepto|tipo|operacion_ref` (sin el saldo).
- Si dos movimientos dan el mismo id (idénticos en esos campos), el segundo lleva `#2`, el tercero `#3`… Se numeran por el saldo y el resto de campos, no por la posición en la lista, así que el orden de la respuesta o el rango pedido no los cambia. Limitación: si más tarde se apunta otro movimiento idéntico el mismo día, la numeración de ese grupo puede cambiar; y se asume que la API filtra `dateFrom`/`dateTo` por fecha de operación (un rango incluye o no el día entero).
- `movements --id ID` busca el movimiento en el rango de `--from`/`--to`/`--days` (por defecto, los últimos 30 días): para uno antiguo, pasa un rango que lo incluya. Si no aparece y falta un tramo que pide firma, sale con 13 en vez de decir que no existe.

Las órdenes (`orders`) usan como `ref` el `reference` de MyInvestor, que según lo observado es único (no se repite entre cuentas), y las aportaciones periódicas (`contributions`) su `contributionId` como `id`: ambos sirven tal cual como clave.

### Posición y cartera: fechas y total

`position` y `holdings` son una foto del momento: en JSON, CSV y TSV cada fila lleva `consultado_en`, la fecha y hora de la consulta en ISO 8601 con zona (`2026-11-03T09:00:12+01:00`); en la tabla va en el título. Así dos fotos se comparan sabiendo cuándo se tomó cada una.

`position` añade `fecha_valor` a cada producto, la fecha a la que corresponde su valor:

| `producto` | `fecha_valor` |
|---|---|
| `efectivo` | hoy (el saldo es en tiempo real) |
| `valores` | el `liquidationValueDate` más reciente de sus fondos, ETF y acciones (`null` si no tiene ninguno) |
| `pensiones` | el `liquidationValueDate` más reciente de sus planes (una petición más por cuenta de pensiones) |

Los fondos suelen ir con el VL de uno o dos días hábiles antes, así que comparar el valor de un lunes con el del viernes puede no reflejar ningún cambio real: mira `fecha_valor` antes de calcular la variación. La última fila es el total, también en JSON/CSV/TSV, con `producto: "total"` (`TOTAL` en la tabla), `valor` = suma de los productos y `fecha_valor: null`. La salida sigue siendo una lista de filas, compatible con lo que había. En `holdings`, la fecha del VL de cada posición ya iba en `fecha_vl`.

`holdings` lleva además `peso`: el % de cada posición sobre el valor de **las posiciones listadas** (todas las cuentas de valores, o las elegidas con `-a` y `--kind`), así que la columna siempre suma 100. Con `--ter` añade `ter`, el de la ficha de cada fondo (`null` en acciones y ETF); cuesta una petición más por fondo distinto, por eso no va por defecto.

### Reparto de la cartera: `allocation`

`allocation` junta los fondos, ETF y acciones de todas las cuentas de valores (también las carteras gestionadas) y los planes de pensiones, los clasifica y los suma por la clave de `--by`:

| `--by` | `clave` | De dónde sale |
|---|---|---|
| `asset` (por defecto) | clase de activo canónica (tabla de abajo), con `origen`: las etiquetas de la API de las que sale | fondos: `assetType` de su ficha; planes: `activeType` de su ficha; acciones y ETF: ver abajo |
| `geo` | zona: `GLOBAL`, `EUROPE`, `UNITED_STATES`, `EMERGENT_MARKETS`… | fondos: `geoZone` de su ficha; acciones, ETF y planes: `sin_clasificar` |
| `category` | categoría | fondos y planes: `category` de su ficha; si no, el `myInvestorCategory` de la posición («Desconocido» cuenta como `sin_clasificar`) |
| `product` | ISIN (o código del plan, o ticker), con `nombre` | la posición |
| `account` | nombre de la cuenta (el de la cartera gestionada, p. ej. `METAL`, o el código), con `cuenta_id` y `tipo` (`valores`, `cartera`, `pensiones`, `efectivo`) | la cuenta |

Cada fila trae `valor`, `peso_pct` (% sobre el total), `invertido`, `ganancia` y `ter`, la media ponderada por valor de las posiciones con TER conocido (solo los fondos: acciones, ETF, planes y efectivo no lo tienen y no lo diluyen). La última fila es el total (`clave: "total"`, `TOTAL` en la tabla) y su `ter` es el **TER medio ponderado de la cartera**. Lo que no tiene clasificación va a `sin_clasificar`, el único marcador de «desconocido» en todas las agrupaciones. En JSON/CSV/TSV cada fila lleva `consultado_en`, como `holdings`.

**Clases de activo.** MyInvestor usa vocabularios distintos según el producto (códigos en los fondos, texto libre en los planes, `RV`/`ETF` en el broker), así que `--by asset` los traduce a un conjunto fijo (`src/myinvestor/asset_classes.py`). Las etiquetas se comparan sin tildes ni mayúsculas:

| Clase | Etiquetas |
|---|---|
| `renta_variable` | `VARIABLE_INCOME`, `RV` (acción en el broker), `STOCK`, «Renta Variable…» |
| `renta_fija` | `FIXED_INCOME`, `RF`, «Renta Fija…» |
| `monetario` | `MONEY_MARKET`, «Mercado monetario», «Monetario» |
| `mixto` | `MIXED`, «Mixto», «Renta Fija Mixta…», «Renta Variable Mixta…» |
| `materias_primas` | `COMMODITIES`, «Materias primas», `ETC` |
| `alternativos` | `ALTERNATIVES`, «Inversión alternativa» |
| `inmobiliario` | «Inmobiliario», `REAL_ESTATE` (no se ha visto ningún código así en la API) |
| `efectivo` | las cuentas de efectivo con `--include-cash` |
| `sin_clasificar` | lo demás («Garantizado», «Desconocido», un ETF cuyo nombre no dice qué replica…); las etiquetas sin traducir se avisan en stderr |

Las **acciones** (`brokerProductType: RV`) son `renta_variable`. Un **ETF o ETC** no se clasifica por el envoltorio sino por lo que replica, leído de su nombre (y de su categoría si la hay) con estas reglas, en orden; `origen` dice qué palabra decidió (`ETF: SILVER`):

1. `MINERS`, `MINING`, `PRODUCERS` → `renta_variable` (empresas del sector: «Gold Miners» no es oro).
2. `GOLD`, `SILVER`, `PLATINUM`, `PALLADIUM`, `PRECIOUS`, `PHYSICAL`, `ETC`, `COMMODIT…`, `ORO`, `PLATA` → `materias_primas`.
3. `MONEY MARKET`, `OVERNIGHT`, `ESTR` → `monetario`.
4. `BOND`, `TREASURY`, `GOVT`, `GOVERNMENT`, `AGGREGATE`, `CORPORATE`, `GILT`, `HIGH YIELD` → `renta_fija`.
5. `MSCI`, `S&P`, `STOXX`, `NASDAQ`, `FTSE`, `DAX`, `IBEX`, `NIKKEI`, `TOPIX`, `RUSSELL`, `EQUITY`, `DIVIDEND` → `renta_variable`.

Si ninguna encaja, `sin_clasificar` (nunca «ETF»). Con `--target`, las claves de `--by asset` son estas clases.

La clasificación sale de la ficha de cada fondo (`/funds/{isin}`, una petición por ISIN distinto aunque esté en varias cuentas). El catálogo (`/funds`) trae lo mismo, pero solo se puede buscar de uno en uno (costaría igual) o recorrer entero (miles de fondos); la ficha, además, responde también por fondos que ya no se comercializan. Las acciones y ETF no piden nada: se clasifican con lo que trae la posición (tipo de producto, nombre y categoría). La ficha del plan de pensiones solo se pide con `--by asset` o `--by category`, que son las que la usan.

Con `--include-cash` el saldo de las cuentas de efectivo (todas, como en `position`) entra como la clase `efectivo` (con `invertido` = saldo y `ganancia` = 0). En `--by account`, la cuenta de efectivo propia de una cartera gestionada (su `cashAccountId`) se suma a esa cartera en vez de salir como otra cuenta.

`--target objetivo.json` compara con unos pesos objetivo, un objeto JSON `{clave: %}` que sume 100, con las mismas claves que `--by` (por ejemplo `{"renta_variable": 80, "renta_fija": 15, "materias_primas": 5}`). Añade `objetivo_pct`, `desviacion_pct` (peso actual − objetivo) y `aportar`, cuánto meter en cada clave para llegar al objetivo **sin vender**: el total final es el menor en el que ninguna clave queda por encima de su objetivo, y la fila total suma lo que hay que aportar. Las claves del objetivo que no tienes salen con `valor` 0. Las que tienes pero no están en el objetivo (o con 0) no se pueden reducir sin vender, así que quedan fuera del cálculo (se avisa en stderr): el peso actual y `aportar` se calculan solo sobre las claves con objetivo.

### Lo que viene: `upcoming`

`upcoming` junta en una tabla, ordenada por fecha, lo que va a mover el efectivo en los próximos `--days` días (14 por defecto, contando hoy):

| `tipo` | De dónde sale | `fecha` |
|---|---|---|
| `aportacion` | aportaciones periódicas activas (`contributions`; se descartan `PAUSED`, `FINISHED` y `CANCELED`) | `nextExecutionDate` y, si cabe más de una en el plazo, las siguientes según la frecuencia (estimadas: no se tienen en cuenta festivos) |
| `orden` | órdenes en curso de las cuentas de valores y de pensiones: estados `INITIAL`, `PENDING` e `IN_PROGRESS`, los mismos que la web da por «en curso» (el resto: `COMPLETE`, `CANCEL`, `REJECTED`, `EXECUTED`) | `orderDate` (nunca más allá del plazo). No se usa `effectiveDate`: en los planes cae un año después de la orden y la web no la muestra |
| `retencion` | `holds` de cada cuenta de efectivo que no casan con ninguna orden (ver abajo); `producto` es un texto legible («retención por orden de inversión») y el `type` de MyInvestor va en `tipo_retencion` | hoy |

Cada fila lleva `{fecha, tipo, producto, importe, cuenta_cargo, retenido, disponible, disponible_tras, falta, tipo_retencion}` (la tabla no muestra `tipo_retencion`). `importe` va con signo, como en `movements`: lo que sale de la cuenta es negativo (una venta en curso, positiva). La cuenta de cargo de una aportación se busca por su `fromAccountId` y, si no casa, por el alias; la de una orden es la cuenta de efectivo asociada a su cuenta de valores o de pensiones. Si no se encuentra, `cuenta_cargo` muestra el alias que da MyInvestor y las columnas de saldo van vacías.

**Órdenes y retenciones.** Una retención solo trae cuenta, `type` e importe, sin referencia a la orden. Por eso cada retención `INVESTMENT_ORDER` se empareja, una a una, con una orden en curso de la misma cuenta de cargo y el mismo importe (`cash` o `amountToBlock`), y las dos salen en una sola fila: la de la orden, con `retenido: true`. Las retenciones que no casan con ninguna orden siguen saliendo como `retencion`.

**Regla del disponible.** `disponible` es el disponible actual de la cuenta de cargo, que MyInvestor ya da descontando lo retenido; y el dinero de una orden en curso queda retenido (o cargado) al darla. Por eso las retenciones y las órdenes se listan pero **no** se restan otra vez: `disponible_tras` solo acumula, por cuenta y en orden de fecha, lo que aún no se ha cargado (las aportaciones). Si baja de cero, `falta` dice cuánto no cubre la cuenta y se avisa en stderr (`-q` lo silencia; el código de salida sigue siendo 0). Con `--owner` solo se ven las filas cargadas a cuentas de ese tipo.

Las transferencias programadas aún no se leen (#4); cuando existan entrarán como `tipo: transferencia` y sí restarán del disponible.

`upcoming` también va, con los próximos 14 días, en la sección `upcoming` de [`snapshot`](#foto-diaria-snapshot-y-summary).

### Foto diaria: `snapshot` y `summary`

`snapshot` junta en **un solo JSON** lo que una rutina diaria necesita, con un solo login/refresh: `total`, efectivo (`cash`, como `accounts`), cuentas de valores con sus posiciones y `peso` (`securities`), planes (`pensions`), órdenes en curso y retenciones (`pending`), lo que viene (`upcoming`), los movimientos recientes (`movements`), lo nuevo desde la vez anterior con el estado de `--new` (`new`) y la variación frente a la foto anterior (`delta`), separando el dinero **aportado** del efecto **mercado**:

```bash
myinvestor -q snapshot --save ~/myinvestor-datos/fotos      # stdout y fotos/AAAA-MM-DD.json (0600), delta frente a la última anterior a hoy
myinvestor snapshot --previous fotos/2026-11-01.json        # delta frente a otra
myinvestor summary                                          # en tablas (no marca lo nuevo como visto)
myinvestor summary --from-file fotos/2026-11-04.json        # una guardada, sin conexión
```

Si una sección falla (un 5xx en pensiones, un tramo de movimientos que pide firma), el resto sale igual, el fallo va a `errors: [{section, error, exit_code, message}]` y el CLI sale con el peor código de las secciones. Esquema completo, fórmula del delta y sus límites (ventas, dividendos…): **[docs/snapshot.md](docs/snapshot.md)**.

### Ingresos: intereses, dividendos, retenciones y comisiones

`income` clasifica los movimientos de las cuentas de efectivo por su `operationType` y los suma por mes (`--by month`, por defecto), año (`--by year`), cuenta (`--by account`) o tipo de movimiento (`--by type`):

| Columna | Qué suma |
|---|---|
| `intereses`, `dividendos` | abonos de intereses (remuneración de la cuenta, depósitos) y de dividendos |
| `bruto` | `intereses` + `dividendos`, tal como se abonan |
| `retenciones` | retenciones a cuenta (IRPF), en negativo |
| `neto` | `bruto` + `retenciones` |
| `comisiones` | comisiones (compraventa, custodia…), en negativo |
| `movimientos` | movimientos que entran en las columnas anteriores |

Los meses se cuentan por la fecha de operación (`fecha`), salen todos los del rango aunque no tengan movimientos y la última fila es el total (`periodo: "total"`). Si MyInvestor abona los intereses ya netos sin un movimiento de retención aparte, `bruto` y `neto` coinciden.

El rango es `--year` (el año natural, hasta hoy si es el actual), `--from`/`--to` o `--days`; por defecto, los últimos 90 días (incluido hoy), que no piden firma. Los movimientos se piden igual que en `movements`, con las mismas opciones: lo anterior hay que aprobarlo en la app, una vez por cuenta; si falta un tramo se muestran los totales de lo obtenido y se sale con 13, salvo con `--allow-partial` (sale con 0 y avisa), y `--free-only` recorta el rango a la ventana sin firma (ver [Movimientos antiguos y segundo factor](#movimientos-antiguos-y-segundo-factor)). Un `income --year` del año pasado pide, por tanto, una aprobación por cuenta.

Categorías (`--by type` muestra cada `operationType` con su categoría y la regla que lo ha clasificado):

| Categoría | Cómo se reconoce |
|---|---|
| `intereses` | «LIQUIDAC. INTERESES» (observado) o cualquier tipo con «INTERES…» o «REMUNERAC…» |
| `dividendos` | «DIVIDEND…» |
| `retencion` | «RETENC…» o «IRPF» (se miran antes que intereses y dividendos: «RETENCION DIVIDENDOS» es retención) |
| `comision` | «COMISION…» o «CUSTODIA» |
| `compra` | «COMPRA RV CONTADO» y «SUSCRIPCION IIC» (observados; acciones/ETF y fondos), «COMPRA» + «RV»/«VALORES», «SUSCRIPC…» |
| `venta` | «VENTA» + «RV»/«VALORES» |
| `aportacion_externa` | transferencias: «CARGO POR TRANSFERENCIA», «ABONO POR TRANSFERENCIA», «TRANSFERENCIA INMEDIATA», «TRANSFERENCIA SEPA» (observados) o cualquier tipo con «TRANSFERENCIA»; el signo del importe dice si entra o sale |
| `traspaso_interno` | los dos lados de un traspaso entre tus cuentas, emparejados como en [Traspasos internos](#traspasos-internos) (regla «traspaso emparejado»; se mira antes que el tipo), o «TRASPASO…» |
| `aportacion_plan` | «APORTACION PLANES» (observado): dinero que sale de la cuenta hacia un plan de pensiones; no es un gasto ni una compra de valores |
| `otros` | «APERTURA» y todo lo demás |

Solo los tipos marcados como observados se han visto en cuentas reales; el resto son palabras clave, porque la API no publica la lista de tipos. Se comparan sin tildes ni mayúsculas, y cada palabra debe empezar una palabra del tipo (`RV` no casa con «SERVICIO»). Los tipos que no casan con nada van a `otros` y el CLI los lista en stderr para poder [ampliar la tabla](src/myinvestor/classify.py). Una transferencia solo es `traspaso_interno` si su otro lado está entre las cuentas consultadas: con `-a` u `--owner`, un traspaso hacia o desde una cuenta que queda fuera sale como `aportacion_externa`, igual que las transferencias de otros clientes.

### Traspasos internos

`internal-transfers` empareja cada cargo de una cuenta tuya con su abono en otra (p. ej. «Mi Cuenta» → cuenta junior). Una fila por traspaso:

| Campo | Notas |
|---|---|
| `fecha`, `importe` | los del cargo; `importe` en positivo |
| `desde_cuenta`, `desde_id` | cuenta (alias) e `id` del movimiento del cargo |
| `hacia_cuenta`, `hacia_id` | cuenta e `id` del movimiento del abono |
| `concepto` | el del cargo (o el del abono si el cargo no trae) |
| `clave` | id de operación sacado de `operationRef`; `null` si no se pudo leer |
| `match` | `key` (por la clave), `heuristic` (respaldo) o `null` (sin pareja, solo con `--unmatched`) |

La tabla muestra `fecha`, `importe`, `desde_cuenta`, `hacia_cuenta`, `concepto` y `match`; JSON y CSV, todo.

- **Por clave** (`key`): dos movimientos son el mismo traspaso si su `operationRef` tiene el formato de traspaso interno (abajo) con el mismo id de operación **y** el mismo bloque numérico (posiciones 68–77), los importes son opuestos y las cuentas distintas.
- Un movimiento con `operationRef` legible solo se empareja por clave. Así, una transferencia de **otro cliente** de MyInvestor (llega como «ABONO POR TRANSFERENCIA», igual que las tuyas) no se confunde con un traspaso tuyo: su id de operación (a veces hasta con otra fecha dentro) no aparece en ninguna de tus cuentas.
- **Heurístico** (`heuristic`), solo si **ninguno** de los dos lados tiene un `operationRef` legible: un «CARGO POR TRANSFERENCIA» y un «ABONO POR TRANSFERENCIA» (nunca una «TRANSFERENCIA SEPA» ni otro tipo) con importes opuestos, en cuentas distintas, como mucho a 1 día y con «Transferencia desde MyInvestor» en el concepto de alguno de los dos. Ante la duda no empareja: si un cargo tiene más de un abono candidato (o un abono más de un cargo), ninguno sale emparejado.
- Un movimiento **nunca** está en dos traspasos.
- `--unmatched` añade los movimientos con pinta de transferencia (`operationRef` legible, ese concepto o un tipo con «TRANSFERENCIA», como los SEPA) que se han quedado sin pareja, con solo `desde_*` (salidas) o solo `hacia_*` (entradas) y `match` vacío: entradas y salidas externas o de otros clientes (p. ej. regalos a la cuenta junior).
- Siempre se leen todas las cuentas; `-a` solo filtra los traspasos que tocan esa cuenta. En `movements`, en cambio, `pareja_id` solo se encuentra si el otro lado está entre las cuentas consultadas (con `-a` u `--owner` puede quedar fuera) y en el rango de fechas.
- Más allá de los ~90 días se comporta como `movements` (ver [Movimientos antiguos y segundo factor](#movimientos-antiguos-y-segundo-factor)): si falta un tramo que pide firma, saca lo obtenido y sale con 13, salvo con `--allow-partial` o `--free-only`.

**Formato de `operationRef`** en un traspaso interno (posiciones desde 0, 93 caracteres):

| Posiciones | Contenido | Ejemplo sintético |
|---|---|---|
| 0–17 | número de tu cuenta, con ceros a la izquierda | `000000000000000001` |
| 18–27 | en blanco en los traspasos internos (cargo y abono); con dígitos en SEPA, compras con tarjeta y transferencias inmediatas | |
| 28–42 | **id de operación**: `1` + fecha `AAAAMMDD` + 6 dígitos; el mismo en los dos lados; es la `clave` | `120261102758295` |
| 43 | espacio | |
| 44–67 | nombre de la entidad (relleno con espacios) | `ENTIDAD GENERICA        ` |
| 68–77 | bloque de 10 dígitos, igual en los dos lados y distinto entre traspasos; se exige que coincida; significado desconocido | `3000000001` |
| 78–83 | contador del movimiento en su cuenta (= `ref`) | `000259` |
| 84–91 | fecha (`AAAAMMDD`) | `20261102` |
| 92 | un dígito; visto `9` en cargos, `7` en abonos internos, `3` en SEPA y `0` en compras y aperturas; significado desconocido | `9` |

En otros movimientos el texto libre empieza en la posición 28 (`MYINVESTOR`, `REMESA:…`, el nombre del valor) y en una «APERTURA» las posiciones 18–67 van en blanco. El `operationRef` solo se da por legible si encaja exactamente: 93 caracteres (o más, solo con espacios detrás); cuenta, id de operación, bloque, contador y último carácter en dígitos; 18–27 en blanco; fechas válidas dentro del id y en 84–91; y el contador igual a `ref` cuando el movimiento lo trae. No se sabe qué significan los 6 últimos dígitos del id de operación, el bloque ni el último dígito.

### Cuentas: tipo, rol y titulares

`accounts` muestra `tipo` (`accountType`), `rol` (el `participantType` del titular que eres tú) y `titular` (el menor en una cuenta junior; si no, el `OWNER`). En JSON añade `titulares: [{"nombre", "rol", "yo"}]`. El DNI de los titulares (`documentNumber`) no se lee ni se exporta nunca.

| Valor | Significado |
|---|---|
| `accountType: CASH_ACCOUNT` | cuenta de efectivo |
| `accountType: CASH_PORTFOLIO` | efectivo asociado a una cartera |
| `participantType: OWNER` | titular (cuentas propias y `CASH_PORTFOLIO`) |
| `participantType: REPRESENTATIVE` | representante: los padres en la cuenta de un menor |
| `participantType: JUNIOR` | el menor en una cuenta junior |

`--owner` (en `accounts` y `movements`) filtra por cuenta: `me` si eres `OWNER`, `junior` si la cuenta tiene un titular `JUNIOR` y `other` para el resto.

### Sin escritorio (cron, Linux)

Guía completa, con crontab, timer de systemd, códigos de salida y un script de ejemplo: **[docs/cron.md](docs/cron.md)**. Lo esencial:

```bash
export MYINVESTOR_STORE=file                      # ~/.config/myinvestor/session.json (0600); el mismo en el cron
myinvestor login --direct --remember -u 12345678Z # una vez, en una terminal: pide la contraseña sin eco
# o, con la contraseña de un gestor de contraseñas:
#   gestor-de-contraseñas | myinvestor login --direct --remember -u 12345678Z --password-stdin
myinvestor -q position -f json                     # semanas después: re-login automático si hace falta
```

La vía recomendada es `--direct --remember` con `--store file` (ver [Estado de `--direct`](#estado-de---direct)): como el refresh token dura 30 min, una ejecución diaria siempre encuentra la sesión caducada y entra de nuevo con la contraseña guardada. El primer login, o cualquiera en el que MyInvestor no reconozca el dispositivo, puede pedir un segundo factor: hazlo una vez en una terminal interactiva con el mismo `--store`, o sin terminal con una de las opciones de abajo (el SMS) o con `--approve` (la aprobación en la app). En el cron no dejes `MYINVESTOR_PASSWORD`, `MYINVESTOR_OTP` ni `MYINVESTOR_APPROVE` (ver la guía).

#### Código SMS sin terminal

Cuando caduca la confianza del dispositivo, MyInvestor puede pedir un **código SMS** en el login `--direct` (también en el re-login automático). El mensaje dice siempre a qué móvil ha ido, enmascarado (`MyInvestor ha enviado un código OTP_SMS a +34 6** *** *12…`), y sale aunque uses `--quiet`. Formas de dárselo sin una terminal en stdin:

- **En dos pasos** (lo más cómodo para un agente o un script que tiene que pedirle el código a una persona, sin mantener el proceso vivo):

  ```bash
  myinvestor login --direct --start -u 12345678Z    # envía el SMS, guarda el login pendiente y sale con 13
  myinvestor login --direct --code 123456           # valida el código y termina el login
  ```

  `--start` deja en el almacén de la sesión (Keychain o fichero 0600) el `signatureRequestId`, el id del SMS enviado, el método, el destino enmascarado y el id de dispositivo. Para terminar, la web repite **la misma llamada de login** (`customerId` + contraseña, mismo dispositivo) con `x-signature-id`, así que `--code` necesita la contraseña otra vez. No se guarda en claro por defecto: `--code` la toma de `--password-stdin`/`MYINVESTOR_PASSWORD`, o de la ya guardada con `--remember`, o la pregunta en la terminal. Con `--start --remember` sí se guarda en el login pendiente (con la misma protección que `--remember`) y pasa a ser la contraseña recordada cuando MyInvestor acepta el código. Si no le pasas contraseña a `--start` y tienes una guardada con `--remember`, la usa: si el cron falla con 13, basta con `--start` y luego `--code`. Un código incorrecto deja el login pendiente para repetir `--code` (3 intentos, como la web); si la firma caduca o MyInvestor la rechaza, se borra y hay que empezar con `--start`. Solo vale para códigos: si MyInvestor solo ofrece aprobarlo en la app, usa `myinvestor --approve login --direct`. Cualquier login correcto o `logout` borra el login pendiente.
- **`--code-stdin`**: el código es la **siguiente línea de stdin**, y se lee solo cuando MyInvestor lo pide (después de la contraseña de `--password-stdin`). Quien lanza el CLI escribe la contraseña, espera al aviso de stderr con el destino y entonces escribe el código. Si el código es incorrecto lee otra línea; si stdin se acaba, falla con 13.
- **`--code-fd 3`**: igual, pero de otro descriptor, para no mezclar secretos en stdin: `myinvestor login --direct --password-stdin --code-fd 3 3< fifo`.
- **`MYINVESTOR_OTP=123456`**: responde a la primera petición de código, **una sola vez**: si MyInvestor lo rechaza no se reintenta (se pasa a `--code-stdin`/la terminal si los hay, o falla con 13). Vale para cualquier orden, también para el re-login automático: con `MYINVESTOR_OTP` definida, el re-login de cron sí envía el SMS y prueba ese código; sin ella, falla sin mandarlo. Ojo: el SMS se envía en esa misma ejecución, así que el código tienes que conocerlo de antemano; para un código que llega después, usa los dos pasos o `--code-stdin`. No la dejes fija en el crontab: cada ejecución enviaría un SMS y probaría un código viejo.
- **`/dev/tty`**: si stdin no es una terminal (p. ej. `echo "$PASS" | myinvestor login --direct --password-stdin`) pero el proceso tiene una, el código se pregunta ahí, como hacen `ssh` o `gpg`. Solo para **códigos**: una aprobación en la app sigue necesitando stdin en una terminal o `--approve`, así que un script lanzado desde una terminal no manda notificaciones al móvil. `/dev/tty` se abre solo cuando hace falta un código y se cierra al acabar. En cron, systemd o un agente desacoplado no hay `/dev/tty`: no se envía el SMS y falla con 13 como siempre.

`--no-signature` gana a todo lo anterior: con él no se inicia ningún segundo factor (ni con `MYINVESTOR_OTP`, `--code-stdin`, `/dev/tty` ni `--start`, que falla con 13 sin mandar el SMS). `--code` sí puede completar un login pendiente, porque el SMS ya se envió.

#### Diagnóstico: `status` y `doctor`

`myinvestor status` confirma la sesión con MyInvestor (lee tu perfil), así que **la renueva** si el token ha caducado (refresh o, con `--remember`, re-login con la contraseña guardada). Con `--offline` solo lee el almacén local: ni llama a la API ni renueva nada, útil como healthcheck barato. Con `-f json` escribe en stdout:

```json
{"logged_in": true, "name": "Ana", "customer_id_masked": "***678Z", "device_id": "…",
 "store": "file", "store_path": "/home/ana/.config/myinvestor/session.json",
 "access_expires_at": "2026-11-03T19:38:00+01:00", "refresh_expires_at": "2026-11-03T19:53:00+01:00",
 "password_remembered": true, "can_refresh": true, "can_relogin": true,
 "relogin_blocked_until": null, "last_relogin_error": null, "verified_online": true}
```

Nunca incluye tokens ni la contraseña, y el DNI/NIE va enmascarado. `logged_in` es «activa o renovable sin `login` manual»; `can_relogin` = contraseña guardada y re-login automático no en pausa (si lo está, `relogin_blocked_until` y `last_relogin_error` dicen hasta cuándo y por qué). `verified_online` solo es `true` si MyInvestor ha respondido (nunca con `--offline`). Sale con 0 si la sesión está activa o es renovable y con **11** si no hay sesión o ha caducado sin forma de renovarla; en ese caso el JSON sale igualmente en stdout, con `logged_in: false`. `-f csv`/`tsv` dan esas mismas claves en una fila.

`myinvestor doctor` da una línea `OK`/`WARN`/`FAIL` por comprobación y sale con **1** si alguna es `FAIL` (0 si solo hay `WARN`):

| Comprobación | Qué mira |
|---|---|
| `store` | tipo de almacén (`keyring`/`file`), ruta, permisos `0600` del fichero y de su `.lock`, que el JSON sea válido |
| `sesion` | tokens, caducidad del access y del refresh token, contraseña guardada sí/no (nunca la muestra), re-login en pausa |
| `red` | `GET` sin autenticar a `api.myinvestor.es/login/api/v1/features`: latencia y bloqueo de Imperva (cualquier otra respuesta, incluso 401, vale) |
| `api` | con la sesión, que el perfil de cliente responda (puede renovarla, como `status`) |
| `reloj` | desfase con la cabecera `Date` del servidor: `WARN` a partir de 30 s (el margen con el que se dan por caducados los tokens), `FAIL` a partir de 5 min |
| `version` | la versión con la que se presenta el CLI (`x-myinvestor-app`) frente a la de la web, que se lee de su script principal o de los que este importa (primero `api-*.js`; como mucho 5, de hasta 8 MB); `WARN` si no coinciden, si está por debajo de la mínima de `settings.json` o si no se puede leer |
| `mantenimiento` | `config.myinvestor.es/flags/settings.json`, el mismo aviso que consulta la web: `FAIL` en mantenimiento, `WARN` en modo solo lectura |

Con `--offline` solo se miran `store` y `sesion`; el resto sale como `WARN` «no comprobado». Con `-f json`: `{"ok": false, "cli_version": "0.2.0", "checks": [{"check": "red", "status": "FAIL", "message": "…", "data": {"http_status": 403, "latency_ms": 85}}, …]}`. Las comprobaciones de red no reintentan y esperan 10 s como mucho; `-v` las muestra.

#### Códigos de salida

Para que un script distinga qué ha pasado sin leer el texto de error (también salen en `myinvestor --help`):

| Código | Caso | Excepciones |
|---|---|---|
| 0 | OK | |
| 1 | Otros errores | `ApiError` (4xx), `LoginRejectedError` (login manual rechazado por otro motivo), `AuthError`… |
| 2 | Uso o datos de entrada incorrectos | opciones de Typer, `InputError` |
| 10 | Contraseña rechazada (la guardada se ha borrado) o cambio de contraseña obligatorio | `InvalidCredentialsError`, `StoredPasswordRejectedError`, `PasswordChangeRequired` |
| 11 | Sin sesión, o caducada sin forma de renovarla (incluido el re-login en pausa) | `NotLoggedInError`, `SessionExpiredError`, `ReloginBlockedError` |
| 12 | Red, WAF (Imperva) o MyInvestor caído: reintentable | `NetworkBlockedError`, errores de `httpx`, `ApiError` 5xx tras los reintentos |
| 13 | Hace falta un segundo factor que no se puede dar sin terminal, `login --direct --start` ha enviado el código, o `movements`, `income` o `export` han devuelto solo una parte (ver abajo) | `SignatureRequired`, `SignatureCodeRequired`, `WrongCodeError`, `SignatureError`, `PartialResultError` |
| 14 | Sesión guardada corrupta | `CorruptStoreError`, `CorruptKeyringError` |

`snapshot` y `summary` escriben la foto aunque falle alguna sección y salen con el peor código de las secciones, en el orden 13, 1, 2, 12; una sesión inservible (10, 11, 14) aborta sin escribir nada (ver [docs/snapshot.md](docs/snapshot.md#errores-por-sección-y-código-de-salida)).

Con `-f json` el error también sale en JSON, en una sola línea de stderr: `{"error": "signature_required", "exit_code": 13, "message": "…"}` (`error` es el nombre de la excepción en snake_case sin `Error`; `network` para los errores de red).

### Movimientos antiguos y segundo factor

La consulta de movimientos solo sale directa para los últimos ~90 días (PSD2). Para rangos más antiguos MyInvestor responde `202` con una petición de firma y pide **aprobarla en la app del móvil**. En una terminal interactiva el CLI lo avisa y espera (`--approval-timeout`, 300 s por defecto) y después repite la consulta firmada. Sin terminal (cron, CI, el `!` de Claude Code) **no** te manda la notificación, salvo que pases `--approve` (o `MYINVESTOR_APPROVE=1`): `myinvestor --approve movements --from 2025-01-01`. `--approve` solo cubre la aprobación en la app (INAPP): no sirve para un código SMS, tampoco en el login `--direct`; para darle un código sin terminal, ver [Código SMS sin terminal](#código-sms-sin-terminal).

**Límite observado** (no verificado al día exacto, porque cada prueba crea una petición de firma en tu cuenta): con `dateFrom` hasta 89 días antes de hoy la consulta va directa; a partir de unos 90 días pide firma. No se sabe si cuenta desde hoy o desde `dateTo`. El CLI se queda en el lado seguro: la ventana sin firma son los últimos 90 días naturales contando hoy (`--from` ≥ hoy − 89).

**Rangos largos, en tramos.** Si el rango empieza antes de esa ventana, `movements` lo parte por días completos: primero pide `[hoy − 89, --to]` de **todas** las cuentas y después el tramo antiguo, con **una sola firma por cuenta**. El tramo antiguo solo se pide si hay forma de firmar (terminal interactiva o `--approve`); si una firma se rechaza o caduca, no se piden las de las demás cuentas. Lo ya obtenido nunca se pierde: se muestra lo que haya y, si falta algún tramo, el CLI sale con **13** (`partial_result` con `-f json`) y dice qué cuentas y fechas faltan. Con `--allow-partial` sale con 0 y lo avisa en stderr. Los `id` se numeran juntando los tramos de cada cuenta, así que no cambian por trocear.

- `--free-only` (o `--clip`): no pide nada que necesite firma; recorta `--from` a hoy − 89 y lo avisa en stderr. Si todo el rango es anterior, error de uso (2).
- `--no-signature` (opción global, o `MYINVESTOR_NO_SIGNATURE=1`): no inicia **nunca** un segundo factor, ni en una terminal ni con `--approve` (que queda sin efecto). Sirve para cualquier comando; en `movements` equivale a no tener terminal: lo libre sale y lo demás acaba en 13.
- En JSON/CSV la salida sigue siendo la lista de filas de siempre: el recorte y el resultado parcial solo se avisan en stderr (silenciable con `-q`), y el resultado parcial además con el código de salida.

```bash
myinvestor --no-signature movements --days 365 -f json --allow-partial > movs.json   # cron: lo que se pueda, sin firmas
myinvestor movements --free-only --from 2025-01-01 -f csv                            # sin firma, recortado
myinvestor --approve movements --from 2025-01-01 -f json                             # todo, aprobando en la app
```

### Solo lo nuevo: `--new`, `--since-date` y `--since-id`

Para una revisión diaria (cron, un agente) que solo debe enseñar lo que ha cambiado desde la vez anterior:

```bash
myinvestor movements --new -f json                 # movimientos que no salieron en el anterior --new
myinvestor orders --new                            # órdenes nuevas o que han cambiado de estado (IN_PROGRESS → EXECUTED)
myinvestor orders --pension --new                  # lo mismo con las de planes de pensiones
myinvestor contributions --new                     # aportaciones nuevas o con otro estado o próxima fecha (= se ha ejecutado)
myinvestor movements --since-date 2026-11-01       # posteriores al 1 de noviembre (sin incluirlo)
myinvestor movements --since-id "<id>"             # posteriores a ese movimiento, en su cuenta
```

**`--new`** guarda un estado local en `$XDG_STATE_HOME/myinvestor/seen.json` (por defecto `~/.local/state/myinvestor/seen.json`; fichero 0600, directorio 0700, escritura atómica con `flock` como la sesión). Otra ruta con `--state PATH` o `MYINVESTOR_STATE`.

- **Movimientos**: por cuenta, el último día consultado y los `id` ya mostrados. La siguiente vez pide desde ese día **menos 3 días** (hay movimientos que aparecen con unos días de retraso) hasta hoy, y solo saca los `id` que no ha visto. Los `id` con fecha anterior a ese inicio ya no se van a volver a pedir, así que se borran del estado: el fichero no crece sin fin. La primera vez (cuenta sin estado) pide los últimos 30 días, o `--from`/`--days`.
- **Nunca sale de la ventana sin firma**: si el estado de una cuenta es más antiguo que los últimos 90 días (la rutina lleva tiempo sin ejecutarse), se consulta desde el inicio de la ventana y se avisa en stderr de que lo anterior no se ha revisado (pídelo a mano con `--from`). Tampoco la primera vez: un `--from`/`--days` más antiguo se recorta y se avisa.
- **Órdenes** (`orders --new`, `orders --pension --new`): se guarda `ref → estado`; sale una orden si su `ref` es nueva o su estado ha cambiado. Se aplica a lo que devolvería `orders` con las mismas opciones (sin fechas, las recientes; `--status` filtra antes de comparar).
- **Aportaciones** (`contributions --new`): se guarda `id → {estado, proxima}`; sale si es nueva o ha cambiado su estado o su próxima fecha (cuando se ejecuta, la próxima fecha avanza).
- `--new` no se combina con `--to`, `--search`, `--id`, `--since-date` ni `--since-id` en `movements`: el estado avanza hasta hoy y filtrar dejaría movimientos sin enseñar que ya no se volverían a pedir.

**Leer sin perder nada**: por defecto lo mostrado queda marcado como visto (`--mark-seen`). Con `--no-mark` se muestra pero queda **pendiente**, y se confirma después con `myinvestor state mark [movements|orders|pension_orders|contributions]`; así, si la rutina falla a medias, la siguiente ejecución lo vuelve a sacar:

```bash
myinvestor movements --new --no-mark -f json > nuevos.json && procesar nuevos.json && myinvestor state mark movements
```

`myinvestor state show` resume el estado (por cuenta: último día, `id` vistos y pendientes; con `-f json`, el fichero completo) y `myinvestor state reset [tipo]` lo borra (todo, o solo un tipo): la siguiente vez con `--new` todo sale como nuevo. `state reset` también sirve si el fichero está corrupto.

**`--since-date AAAA-MM-DD`**: movimientos con `fecha` (de operación) **posterior** a ese día; equivale a `--from` del día siguiente. No se combina con `--from`/`--days`.

**`--since-id ID`**: los movimientos de la cuenta de ese `id` que van **después** de él. «Después» es el orden cronológico del listado: primero por `fecha` (de operación) y, el mismo día, por `ref` (el contador de MyInvestor, por cuenta) y luego por `id`. El movimiento se busca, como con `--id`, en el rango de `--from`/`--days` (por defecto, los últimos 30 días); si no está, error de uso (o 13 si falta un tramo que pide firma). Limitación: un movimiento que se apunte más tarde con una `fecha` anterior no sale; para eso está `--new`, con su margen de 3 días.

### Exportar el histórico: `export`

```bash
myinvestor export ~/finanzas/myinvestor                    # la primera vez, todo (una aprobación en la app por cuenta)
myinvestor -q export ~/finanzas/myinvestor                 # cada día desde cron: solo añade lo nuevo, sin firmas
myinvestor export DIR --since 2024-01-01 --format json     # desde una fecha; JSON Lines
```

Escribe en `DIR` (si no existe, se crea con permisos 0700) un fichero por conjunto, 0600, con las mismas columnas que la orden correspondiente en CSV/JSON:

| Fichero | Contenido | Una fila nueva cuando… |
|---|---|---|
| `movements` | movimientos de todas las cuentas de efectivo (columnas de `movements`, con `id` estable y `pareja_id`/`traspaso_interno`, emparejados dentro de lo pedido en cada ejecución) | aparece un `id` nuevo |
| `orders`, `pension_orders` | órdenes de valores y de planes (`orders`, `orders --pension`) | aparece una `ref` nueva o la misma con otro `estado` (la última fila de cada `ref` es la vigente) |
| `contributions` | aportaciones periódicas, también las canceladas (`contributions --all`) | cambia el `estado`, la `proxima` fecha, el `importe` o la `frecuencia` |
| `holdings` | cartera de valores (`holdings`, con `peso`; sin `--ter`) con `consultado_en` | primera exportación del día (una foto por día) |
| `accounts` | cuentas de efectivo (`accounts`) con `consultado_en` | primera exportación del día (una foto del saldo por día) |

Con `--format csv` (por defecto) son `.csv` con cabecera; con `--format json`, **JSON Lines** (`.jsonl`, un objeto por línea, importes como números), porque un array JSON no se puede ampliar sin reescribirlo. Un directorio se queda con el formato de su primera exportación.

**Incremental y sin reescribir.** Cada ejecución lee los ids que ya hay, añade al final solo las filas nuevas y nunca toca las antiguas: el fichero nuevo se escribe en un temporal con el contenido anterior tal cual más las filas nuevas y se renombra encima (escritura atómica, con `flock` en `DIR/.export.lock` para que dos ejecuciones no se pisen). Si no existe, se crea con cabecera. Si una versión posterior del CLI añade columnas, se mantiene la cabecera que ya tiene el CSV y se avisa en stderr de las columnas que no se guardan; para tenerlas, exporta a otro directorio.

**Rango de los movimientos.** La primera vez, por cuenta, desde `--since` o, si no se indica, desde el 2010-01-01; lo anterior a los últimos 90 días se pide en un solo tramo por cuenta, con **una aprobación en la app por cuenta**, solo en una terminal interactiva o con `--approve` (como en [Movimientos antiguos y segundo factor](#movimientos-antiguos-y-segundo-factor)). Las siguientes veces, desde el último día exportado menos 3 días, así que llamándolo a diario (o al menos cada 90 días) nunca pide firma y el histórico local acaba cubriéndolo todo. En esas ejecuciones, `--since` obliga a pedir al menos desde ese día (p. ej. para ampliar hacia atrás). Las órdenes se piden siempre desde `--since` (o 2010-01-01) para ver sus cambios de estado.

**Sin forma de firmar** (cron sin `--approve`, `--no-signature`): exporta lo que se da sin firma y todo lo demás, anota el tramo que falta como hueco en el manifest y sale con **13**, o con 0 y un aviso con `--allow-partial`. La siguiente exportación vuelve a pedir ese hueco (y, si sigue sin poder firmar, vuelve a acabar en 13).

**`manifest.json`** se reescribe (atómicamente) en cada ejecución:

```json
{"version": 1, "as_of": "2026-11-03T09:00:12+01:00", "cli_version": "0.2.0", "format": "csv",
 "files": {"movements": {"fichero": "movements.csv", "filas": 1520}, …},
 "accounts": {"<cuenta_uuid>": {"cuenta": "Mi cuenta", "cuenta_id": "…",
   "cubierto": [{"desde": "2010-01-01", "hasta": "2026-11-03"}],
   "huecos": [{"desde": "…", "hasta": "…", "motivo": "firma"}],
   "ultimos_tramos": [{"desde": "2026-08-06", "hasta": "2026-11-03", "movimientos": 42, "saldo_inicial": 1000.00,
                       "suma_importes": -50.00, "saldo_final": 950.00, "cuadra": true}]}}}
```

- `cubierto`: los días de cada cuenta que ya están en `movements`, en intervalos.
- `huecos`: lo que falta dentro de lo pedido. `firma`, un tramo que pedía segundo factor y no se pudo dar (se reintenta solo en la siguiente exportación); `descuadre`, un tramo en el que `startBalance + Σ importes ≠ endBalance`, es decir, en el que MyInvestor no ha dado todos los movimientos. Los descuadres se avisan en stderr pero no cambian el código de salida; se vuelven a pedir con `--since`.
- `ultimos_tramos`: la comprobación de saldos de cada tramo pedido en esta ejecución (`cuadra: null` si la respuesta no traía saldos).
- Las cuentas que dejan de aparecer conservan su entrada.

### Silencio, depuración y ritmo de peticiones

Opciones globales (van antes de la orden: `myinvestor -q position -f json`):

- `-q`/`--quiet` (o `MYINVESTOR_QUIET=1`): en stderr solo los errores; se silencian los avisos («Rango de más de 90 días…», «Sin resultados», «Sesión iniciada…»). El código de salida sigue indicando si ha ido bien. Pensado para cron: el log diario se queda con el JSON y los fallos.
- `-v`/`--verbose`: una línea por petición en stderr, `GET /ruta 200 (123 ms)`, marcando `[refresh]`, `[login]`, `[firma]` (endpoints de segundo factor) o `[firmada]` (consulta repetida tras aprobarla), y cuándo se renueva la sesión o se hace re-login. **Nunca** se escriben la query, las cabeceras (`authorization`, `x-signature-id`…), los cuerpos de login/refresh, tokens, contraseñas ni códigos; los ids de firma se enmascaran (`/signature/api/v3/signature/*`). Las rutas sí llevan ids de cuenta: revísalo antes de pegarlo en un issue.
- `--min-interval SEG` (o `MYINVESTOR_MIN_INTERVAL`, por defecto 0): espera mínima entre peticiones del mismo proceso, para no despertar al WAF con muchas cuentas.
- `--json-envelope` (o `MYINVESTOR_JSON_ENVELOPE=1`): con `-f json`, la salida envuelta con su versión de esquema (ver [docs/json.md](docs/json.md)).
- `--retry-blocked N` (o `MYINVESTOR_RETRY_BLOCKED`, por defecto 0): si Imperva bloquea una consulta `GET`, espera 30 s, 2 min, 5 min (y 5 min las siguientes) y la repite hasta N veces. Login, refresh, firmas y cualquier `POST` no se repiten nunca.

stdout solo lleva el resultado: con `-f json` y sin datos sale `[]`, con `-f csv` o `-f tsv` sale vacío, y en ninguno se escribe «Sin resultados». Sin terminal en stderr o con `NO_COLOR` no hay colores ni marcado (lo hace rich).

Dentro de una misma orden, las consultas `GET` idénticas (misma ruta y parámetros) se responden de memoria; las firmadas y las `202` no se guardan, y nada se guarda en disco.

Peticiones que hace cada orden (V = cuentas de valores activas, P = cuentas de pensiones, C = cuentas de efectivo elegidas, por defecto todas, F = fondos distintos en cartera, cuya ficha se pide una vez, D = planes de pensiones distintos):

| Orden | Peticiones |
|---|---|
| `accounts`, `balance-history`, `contributions`, `funds`, `feed`, `raw`, `status` | 1 |
| `position` | 3 + V |
| `movements`, `holds` | 1 + C (`movements --new`: igual; `--since-id`: 1 + 1) |
| `export` | 4 + C + 2V + P; la primera vez (o con un hueco), + C del tramo antiguo y sus firmas |
| `holdings` | 1 + V (también con `-a`: se leen todas para resolverla); `--ter`: + F |
| `allocation` | 2 + V + P + F; con `--by asset` o `category`: + D; con `--include-cash`: + 1 |
| `orders` | 1 + V; con `-a`: 1 + V + las elegidas; `--pension`: 1 + P |
| `pensions` | 1 + P |
| `upcoming` | 4 + C + 2·V + P (C: con `--owner`, solo las de ese tipo) |
| `snapshot`, `summary` | 4 + 2·C + 2·V + 2·P (`summary --from-file`: 0) |
| `history` | sin argumentos: 2 + V + P; con ISIN: 2 + V; con `N1234`: 1 + P; con `-a` de pensiones: 2; con `-a` de valores: 3 + V |
| `fund` | ISIN: 2 (1 con `--full`); plan `N1234`: 1 |
| `login` | `--direct` o `--refresh-token-stdin`: 1 |

Además: +1 `POST` de refresh si el token (15 min) ha caducado o MyInvestor responde 401, y +1 `POST` de login si hay que hacer re-login con la contraseña guardada. Cada segundo factor suma 1 `POST` para iniciarlo, la validación del código o una consulta de estado cada 3 s mientras esperas en la app, y la repetición firmada. Los reintentos ante `429/5xx` (hasta 4) y los de `--retry-blocked` van aparte.

## Como librería

```python
from datetime import date
from myinvestor import KeyringSessionStore, MyInvestorClient

store = KeyringSessionStore()
with MyInvestorClient(store.load(), store=store) as client:
    for account in client.cash_accounts():
        statement = client.statement(account.account_uuid, date(2026, 10, 1), date(2026, 10, 31))
        for flow in statement.flows:
            print(flow.operation_date, flow.concept, flow.amount)
```

## Diseño

- **Importes en `Decimal`**: la API devuelve euros como números JSON en coma flotante; se parsean directamente a `Decimal` (`json.loads(parse_float=Decimal)`), sin redondeos binarios. Los movimientos ya vienen con signo (cargos < 0) y `startBalance + Σ importes = endBalance`.
- **Reintentos**: `GET` se reintenta 4 veces con backoff exponencial y jitter ante `429/5xx`, timeouts y errores de red, respetando `Retry-After`. Login, refresh y firmas (`POST`) **nunca** se reintentan. El bloqueo de Imperva no se reintenta salvo con `--retry-blocked` (en la librería, `MyInvestorClient(..., retry_blocked=N)`), y solo en consultas `GET`.
- **Caché y ritmo**: `MyInvestorClient(..., cache=True)` (lo que usa el CLI) memoriza las respuestas `GET` mientras vive el cliente; por defecto está desactivada para que un cliente de larga duración no sirva datos viejos. `build_http_client(min_interval=SEG)` espacia las peticiones.
- **Log**: cada petición se registra en `DEBUG` en el logger `myinvestor.http` (método, ruta, status, ms), nunca con query, cabeceras ni cuerpos.
- **Cabeceras como la web**: `authorization: Bearer`, `x-device-id`, `x-myinvestor-app: version=3.134.0,platform=web`, y `origin`/`referer`/`user-agent` de Chrome, porque Imperva está delante.
- **Fechas**: la API manda medianoches ISO (`2026-11-02T00:00:00.000+0000`); se usan como día natural.

## API observada

Base `https://api.myinvestor.es`. Todas las respuestas usan el sobre `{"payload": {"data": …, "pagination"?: …}, "status": {"code", "message", "timestamp"}}`. `app.myinvestor.es` (la API antigua `/myinvestor-server/rest/…`) responde 403 a todo.

### Autenticación (Keycloak `myinvestor-prod`)

| Método | Ruta | Notas |
|---|---|---|
| POST | `/login/api/v2/auth/token` | `{customerId, password}` + `x-device-id`; la web añade `x-recaptcha-token` y `x-recaptcha-action: LOGIN_V2`. → `{accessToken, refreshToken, expiresIn: 900, refreshExpiresIn: 1800}`, o `202` con petición de firma (dispositivo nuevo); se repite con `x-signature-id`. `isPasswordChangeRequired`, `errCode` en errores (la web conoce `UNAUTHORIZED` = contraseña incorrecta y `UPDATE_PASSWORD_REQUIRED`) |
| POST | `/login/api/v1/auth/token/refresh` | `{refreshToken}`, sin `authorization` → par nuevo |
| GET | `/login/api/v1/features[/self]` | flags (`SIGNIN_CHECK_DEVICE_ID`…) |

### Firmas (segundo factor)

Una llamada protegida puede responder `202` con `{signatureRequestId, status: PENDING, pendingSignatureMethods: [["INAPP"], …]}` (grupos de métodos alternativos). Antes del login, las mismas rutas con el prefijo `/signature/api/v3/public/signature`.

| Método | Ruta | Notas |
|---|---|---|
| POST | `/signature/api/v3/signature/{id}?method=INAPP\|OTP_SMS\|…` | inicia el método → `{signatureId, type, channel?, destination?}` |
| POST | `/signature/api/v3/signature/{id}/validate/{signatureId}` | `{code}` para OTP |
| GET | `/signature/api/v3/signature?signatureRequestId=…` | estado (`PENDING`, `COMPLETE`, `CANCELLED`, `EXPIRED`, `ERROR`); la web lo sondea cada 3 s para INAPP |

Métodos que existen pero no se soportan: `PERSONAL_SIGNATURE`, `FACE_AUTH`, `SMILEID`, `PASSWORD`, `CAPTCHA`.

### Lectura

| Método | Ruta | Notas |
|---|---|---|
| GET | `/cperf-server/api/v3/customers/self` | perfil |
| GET | `/cperf-server/api/v2/cash-accounts/self` | `accountId`, `accountUuid`, `alias`, `iban`, `accountType`, `enabledBalance`, `withheldBalance`, `holders` (`name`, `surname1`, `surname2`, `documentNumber`, `me`, `participantType`) |
| GET | `/account/api/v3/account/{accountUuid}/flows?dateFrom&dateTo` | `{flowList, iban, startBalance, endBalance}`; cada flow: `amount, concept, currency, fileRef, flowType, isMigrated, operationClass, operationDate, operationOriginLabel, operationRef, operationTargetLabel, operationType, reference, totalBalance, valueDate`; más de ~90 días → `202` + firma INAPP |
| GET | `/account/api/v3/account/{accountUuid}/holds` | retenciones |
| GET | `/cperf-server/api/v2/cash-accounts/historical-balance?accountTypes=CASH_ACCOUNT` | `[{date, balance}]` |
| GET | `/cperf-server/api/v2/securities-accounts/self-basic` | ids de cuentas de valores |
| GET | `/cperf-server/api/v2/securities-accounts/{id}` | totales + `securitiesAccountInvestments` por tipo (`INDEXED_FUND`, `BROKER`…) con `investmentList` |
| GET | `/cperf-server/api/v3/securities-accounts/{id}/orders[?dateFrom&dateTo]` | sin fechas, solo recientes; con fechas, el histórico (la v2 con fechas da 500) |
| GET | `/cperf-server/api/v2/{securities,pension}-accounts/{id}/positions?groupType=` | serie diaria por ISIN/código; `ONE_WEEK`, `ONE_MONTH`, `THREE_MONTHS`, `SIX_MONTHS`, `ONE_YEAR`, `THREE_YEAR`, `MAX` |
| GET | `/cperf-server/api/v2/securities-accounts/{id}/investment-summary` | totales fondos/acciones |
| GET | `/cperf-server/api/v2/pension-accounts/self` · `/{id}` · `/{id}/orders?order=DATE` | planes y sus aportaciones |
| GET | `/cperf-server/api/v2/pension-plans/{dgsCode}` | ficha del plan |
| GET | `/cperf-server/api/v2/automatic-contributions/self` | aportaciones periódicas |
| GET | `/cperf-server/api/v2/funds?page&limit&sort&mainInputFilter&…` | catálogo paginado; filtros `category`, `region`, `manager`, `assetsType`, `currency`, `terMinValue`/`terTopValue`, `riskMinValue`/`riskTopValue`… |
| GET | `/cperf-server/api/v2/funds/{isin}` · `/prices/last` · `/compositions` · `/graphs/{type}` | ficha, último VL |
| GET | `/feed/api/feed/self` | `text/event-stream` con un evento `data:{…}` |

**Fuera de alcance a propósito**: todo lo que escribe (`…/funds/buy-orders`, `selling-orders`, `inner-transfers`, `external-transfers`, `cash-accounts/{id}/transfers`, cancelar órdenes, aportaciones, Bizum…).

## Desarrollo

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Los tests usan `respx` con datos sintéticos y bloquean la red; no se versiona ninguna respuesta real.

`tests/test_json_contract.py` comprueba la salida `-f json` de cada orden contra la tabla de `src/myinvestor/json_contract.py`. Si cambias claves, actualiza la tabla (y sube la versión del esquema si el cambio es incompatible, ver [docs/json.md](docs/json.md)) y ejecuta `UPDATE_JSON_CONTRACT=1 uv run pytest tests/test_json_contract.py`, que regenera la referencia de `docs/json.md` y `tests/json_contract.lock.json` (nunca acepta un cambio incompatible en un esquema ya publicado).
