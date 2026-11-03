# Salida JSON: el contrato

Qué claves y tipos saca cada orden con `-f json`, cuáles son estables y cómo se anuncia un cambio. La referencia por orden (al final) se genera de la tabla de `src/myinvestor/json_contract.py`, la misma que usan los tests de contrato (`tests/test_json_contract.py`) y `myinvestor schema`, así que no puede desfasarse del código.

## Versión de esquema y `--json-envelope`

Cada orden tiene un esquema con versión: `myinvestor.<orden>/<n>` (`myinvestor.movements/1`, `myinvestor.state-show/1`…). Con la opción global `--json-envelope` (o `MYINVESTOR_JSON_ENVELOPE=1`), la salida va envuelta:

```bash
myinvestor --json-envelope movements -f json
```

```json
{
  "schema": "myinvestor.movements/1",
  "cli_version": "0.2.0",
  "as_of": "2026-11-03T09:00:12+01:00",
  "data": [{"id": "…", "fecha": "2026-11-02", "importe": -12.50, …}]
}
```

- `schema`: nombre y versión del esquema de `data`. Un script puede comprobarlo y negarse a seguir si no es el que conoce.
- `cli_version`: versión del CLI.
- `as_of`: fecha y hora (ISO 8601 con zona) en que empezó la ejecución. En `position`, `holdings` y `allocation`, `consultado_en` de cada fila es la de la consulta, un instante después.
- `data`: exactamente lo que la orden saca sin la opción: una lista, o un objeto en `status`, `doctor`, `state show` y `movements --id` (en `raw` y `fund`, la respuesta de la API).

**Sin la opción la salida no cambia**: es la misma lista u objeto de siempre, por compatibilidad. El envoltorio solo afecta a stdout con `-f json` (y a `raw`/`fund`, que siempre sacan JSON); `table`, `csv` y `tsv` no cambian, y la [línea de error](#errores) de stderr nunca se envuelve.

`myinvestor schema` lista `{orden: esquema}`; `myinvestor schema movements` imprime el JSON Schema (draft 2020-12) de la salida de `movements`, y con `--envelope`, el de la salida envuelta. `myinvestor schema error` da el de la línea de error.

## Qué es estable y qué es un cambio incompatible

Dentro de una misma versión de esquema:

- **No cambian**: el nombre de las claves, su tipo, si pueden ser `null`, y la forma de `data` (lista u objeto).
- **Pueden aparecer claves nuevas.** Añadir una clave es compatible: no sube la versión, pero se anota en el `CHANGELOG.md`. Un script debe ignorar las claves que no conoce (el JSON Schema lo permite: `additionalProperties: true`).
- Quitar o renombrar una clave, cambiar su tipo o hacer que admita `null` es **incompatible**: sube el número de esquema (`/1` → `/2`) y se anota en el `CHANGELOG.md`. Los tests de contrato fallan si una salida no cumple su tabla, y si la tabla cambia de forma incompatible sin subir la versión (`tests/json_contract.lock.json` guarda cada esquema publicado).
- Las claves marcadas como **no estables** (columna «Estable») siguen ahí, pero su contenido es informativo y puede cambiar: textos para personas (`message`) y detalles libres (`checks[].data` de `doctor`).
- El orden de las claves dentro de un objeto no forma parte del contrato (en la práctica no cambia). El de las filas sí está documentado en cada orden cuando importa (p. ej. la fila `total`, siempre la última).

## Reglas generales

- **Claves en español, en minúsculas, sin tildes y con `_`** (`fecha`, `valor_fecha`, `importe`, `cuenta_uuid`). Excepciones históricas, que se mantienen por compatibilidad: `status` y `doctor` usan claves en inglés, y `state show` muestra el fichero de estado tal cual.
- **Importes en euros** (`importe`, `saldo`, `disponible`, `valor`, `invertido`, `ganancia`…, marcados como «number (€, 2 decimales)»): número JSON (nunca texto), **siempre con 2 decimales** (`-50.00`, `1000.30`), redondeado a céntimos (mitad hacia arriba) y sin `-0.00`.
- **Precios, participaciones y porcentajes** (`vl`, `coste_medio`, `participaciones`, `rentab`, `ter`…, «number (exacto)»): número JSON con la precisión que da la API, sin redondear ni pasar por coma flotante (`12.345678`). Los porcentajes van en % (`10.06` es un 10,06 %). Los que calcula el CLI (`peso`, `peso_pct`, `desviacion_pct`) van a 2 decimales.
- Los números se escriben con el valor exacto del `Decimal`. En Python, `json.loads(texto, parse_float=Decimal)` los recupera sin pérdida; `jq` (1.7+) conserva el literal si no opera con él.
- **Fechas**: `AAAA-MM-DD` («date»). **Fecha y hora**: ISO 8601 al segundo con la zona (`2026-11-03T09:00:12+01:00`, «datetime»).
- **`null` cuando la API no trae el dato**: el CLI no rellena nada por su cuenta. Una clave nunca desaparece por ser `null`; solo las marcadas «solo con `--opción`» dependen de una opción.
- **Booleanos**: `true`/`false` (`traspaso_interno`, `retenido`, `titulares[].yo`…).
- **Sin resultados**: `[]` (lista vacía), nunca un texto.
- **Filas de total**: `position`, `income` y `allocation` acaban con una fila de total, con `total` en su primera clave (`producto`, `periodo`/`categoria`/`cuenta` o `clave`).

## Errores

Con `-f json`, un error sale en **una sola línea JSON en stderr** (stdout queda vacío o con lo obtenido antes del fallo), y el proceso sale con el código de su tipo (ver «Códigos de salida» en el README):

```json
{"error": "signature_required", "exit_code": 13, "message": "…"}
```

`error` es el nombre de la excepción en snake_case sin `Error` (`network` para los errores de red), estable; `message` es texto para personas. Su esquema es `myinvestor.error/1` (`myinvestor schema error`), pero nunca va envuelto.

## Fuera del contrato

- **`raw` y `fund`** devuelven la respuesta de MyInvestor tal cual (claves camelCase de la API, que puede cambiarlas cuando quiera). Con `--json-envelope` van con el esquema `myinvestor.raw/1`, que solo garantiza el envoltorio: `data` es lo que responda la API. `fund` sin `--full` se limita a unas claves de la ficha (`name`, `isinCode`, `ter`, `liquidationValue`… y `lastPrice`), y omite las vacías.
- **`export`** escribe ficheros, no stdout: sus filas tienen las mismas claves que `movements`, `orders`, `contributions`, `holdings` (sin `ter`) y `accounts` (sin `titulares` y con `consultado_en`), y `manifest.json` lleva su propio campo `version`.
- Los formatos `table`, `csv` y `tsv` no forman parte de este contrato (CSV y TSV tienen las mismas columnas que el JSON, salvo listas como `titulares`).

## Referencia por orden

Columnas: **Clave** (`a[].b`: clave `b` de cada objeto de la lista `a`), **Tipo**, **`null`** (si puede valer `null`), **Estable** y **Notas** (de dónde sale en la API, unidades, valores posibles).

<!-- BEGIN: generado de src/myinvestor/json_contract.py; no editar a mano -->

### `position` — `myinvestor.position/1`

Una fila por producto y, al final, el total.

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `producto` | string | no | sí | `efectivo`, `valores`, `pensiones` o `total` (última fila) |
| `id` | string | no | sí | id de la cuenta; `""` en el total |
| `nombre` | string | sí | sí | `""` en el total |
| `valor` | number (€, 2 decimales) | sí | sí |  |
| `invertido` | number (€, 2 decimales) | sí | sí | `null` en efectivo y en el total |
| `ganancia` | number (€, 2 decimales) | sí | sí | `null` en efectivo y en el total |
| `rentab` | number (exacto) | sí | sí | en %; `null` en efectivo y en el total |
| `fecha_valor` | date `AAAA-MM-DD` | sí | sí | VL más reciente del producto (hoy en efectivo; `null` en el total) |
| `consultado_en` | datetime ISO 8601 con zona | no | sí | fecha y hora de la consulta (la misma en todas las filas) |

### `accounts` — `myinvestor.accounts/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `id` | string | no | sí |  |
| `uuid` | string | no | sí |  |
| `alias` | string | sí | sí |  |
| `iban` | string | sí | sí |  |
| `tipo` | string | sí | sí | `accountType` |
| `rol` | string | sí | sí | tu `participantType` |
| `titular` | string | sí | sí | nombre del titular principal (el menor en una cuenta junior) |
| `estado` | string | sí | sí |  |
| `disponible` | number (€, 2 decimales) | no | sí |  |
| `retenido` | number (€, 2 decimales) | no | sí |  |
| `saldo` | number (€, 2 decimales) | no | sí | disponible + retenido |
| `titulares` | array de objetos | no | sí | nunca el DNI |
| `titulares[].nombre` | string | sí | sí |  |
| `titulares[].rol` | string | sí | sí | `participantType` |
| `titulares[].yo` | boolean | no | sí |  |

### `movements` — `myinvestor.movements/1`

Con `--id` sale el objeto del movimiento, no una lista. `--new`, `--since-*` y `--search` solo filtran filas.

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `id` | string | no | sí | id estable: `{cuenta_uuid}:{reference}` (o `:h…` sin reference, `#2` si se repite) |
| `fecha` | date `AAAA-MM-DD` | no | sí | fecha de operación |
| `valor_fecha` | date `AAAA-MM-DD` | sí | sí | fecha valor |
| `cuenta` | string | no | sí | alias de la cuenta (o su id si no tiene); puede cambiar |
| `cuenta_id` | string | no | sí |  |
| `cuenta_uuid` | string | no | sí |  |
| `iban` | string | sí | sí |  |
| `sentido` | string | sí | sí | `flowType`: `IN` / `OUT` |
| `tipo` | string | sí | sí | `operationType` |
| `clase` | string | sí | sí | `operationClass` |
| `concepto` | string | sí | sí |  |
| `importe` | number (€, 2 decimales) | no | sí | con signo: los cargos son negativos |
| `saldo` | number (€, 2 decimales) | sí | sí | saldo de la cuenta tras el movimiento |
| `origen` | string | sí | sí | `operationOriginLabel` |
| `destino` | string | sí | sí | `operationTargetLabel` |
| `contraparte` | string | sí | sí | `destino` en cargos, `origen` en abonos |
| `ref` | string | sí | sí | `reference`: contador por cuenta |
| `operacion_ref` | string | sí | sí | `operationRef`, sin interpretar |
| `fichero_ref` | string | sí | sí | `fileRef` |
| `pareja_id` | string | sí | sí | `id` del otro lado de un traspaso entre tus cuentas |
| `traspaso_interno` | boolean | no | sí |  |

Con `--id`: un objeto.

Las mismas claves que la salida por defecto.

### `holds` — `myinvestor.holds/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `cuenta` | string | no | sí | alias de la cuenta (o su id si no tiene) |
| `id` | string | no | sí |  |
| `tipo` | string | sí | sí | `INVESTMENT_ORDER`… |
| `importe` | number (€, 2 decimales) | no | sí |  |

### `balance-history` — `myinvestor.balance-history/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `fecha` | date `AAAA-MM-DD` | no | sí |  |
| `saldo` | number (€, 2 decimales) | no | sí | total del efectivo |

### `holdings` — `myinvestor.holdings/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `cuenta` | string | no | sí | id de la cuenta de valores |
| `tipo` | string | sí | sí | `investmentProductType`: `INDEXED_FUND`, `BROKER`… |
| `isin` | string | sí | sí |  |
| `nombre` | string | sí | sí |  |
| `participaciones` | number (exacto) | sí | sí |  |
| `coste_medio` | number (exacto) | sí | sí |  |
| `vl` | number (exacto) | sí | sí | valor liquidativo / precio |
| `fecha_vl` | date `AAAA-MM-DD` | sí | sí |  |
| `invertido` | number (€, 2 decimales) | sí | sí |  |
| `valor` | number (€, 2 decimales) | sí | sí |  |
| `ganancia` | number (€, 2 decimales) | sí | sí |  |
| `rentab` | number (exacto) | sí | sí | rentabilidad total, en % |
| `peso` | number (exacto) | sí | sí | % sobre el valor de las posiciones listadas, a 2 decimales |
| `ter` | number (exacto) | sí | sí | solo con `--ter`; TER de la ficha del fondo, en %; `null` en acciones y ETF |
| `consultado_en` | datetime ISO 8601 con zona | no | sí | fecha y hora de la consulta (la misma en todas las filas) |

### `orders` — `myinvestor.orders/1`

Las mismas claves con `--pension` y con `--new`.

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `fecha` | date `AAAA-MM-DD` | no | sí |  |
| `cuenta` | string | sí | sí | id de la cuenta de valores o de pensiones |
| `ref` | string | no | sí | `reference`: única |
| `producto` | string | sí | sí | ISIN, código DGS o ticker |
| `nombre` | string | sí | sí |  |
| `operacion` | string | sí | sí | `operationType` |
| `estado` | string | sí | sí | `IN_PROGRESS`, `EXECUTED`… |
| `importe` | number (€, 2 decimales) | sí | sí |  |
| `participaciones` | number (exacto) | sí | sí |  |

### `pensions` — `myinvestor.pensions/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `cuenta` | string | no | sí |  |
| `codigo` | string | no | sí | código DGS del plan (`N1234`) |
| `nombre` | string | sí | sí |  |
| `participaciones` | number (exacto) | sí | sí |  |
| `coste_medio` | number (exacto) | sí | sí |  |
| `vl` | number (exacto) | sí | sí |  |
| `fecha_vl` | date `AAAA-MM-DD` | sí | sí |  |
| `invertido` | number (€, 2 decimales) | sí | sí |  |
| `valor` | number (€, 2 decimales) | sí | sí |  |
| `ganancia` | number (€, 2 decimales) | sí | sí |  |
| `rentab` | number (exacto) | sí | sí | rentabilidad total, en % |

### `history` — `myinvestor.history/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `fecha` | date `AAAA-MM-DD` | no | sí |  |
| `cuenta` | string | no | sí |  |
| `producto` | string | no | sí | ISIN o código DGS |
| `valor` | number (€, 2 decimales) | sí | sí |  |
| `invertido` | number (€, 2 decimales) | sí | sí |  |
| `rentab` | number (exacto) | sí | sí | en % |
| `participaciones` | number (exacto) | sí | sí |  |
| `coste_medio` | number (exacto) | sí | sí |  |

### `contributions` — `myinvestor.contributions/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `id` | string | no | sí | `contributionId` |
| `estado` | string | sí | sí |  |
| `producto` | string | sí | sí | ISIN |
| `nombre` | string | sí | sí |  |
| `importe` | number (€, 2 decimales) | no | sí |  |
| `frecuencia` | string | sí | sí | `ONE_MONTH`… |
| `proxima` | date `AAAA-MM-DD` | sí | sí | próxima ejecución |
| `destino` | string | sí | sí | `toAccountType` |

### `funds` — `myinvestor.funds/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `isin` | string | no | sí |  |
| `nombre` | string | sí | sí |  |
| `gestora` | string | sí | sí |  |
| `categoria` | string | sí | sí |  |
| `ter` | number (exacto) | sí | sí | en % |
| `riesgo` | integer | sí | sí | indicador de riesgo |
| `vl` | number (exacto) | sí | sí |  |
| `ytd` | number (exacto) | sí | sí | rentabilidad en el año, en % |
| `1a` | number (exacto) | sí | sí | rentabilidad a 1 año, en % |

### `feed` — `myinvestor.feed/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `fecha` | date `AAAA-MM-DD` | sí | sí |  |
| `producto` | string | sí | sí |  |
| `tipo` | string | sí | sí |  |
| `titulo` | string | sí | sí |  |
| `detalle` | string | sí | sí |  |

### `internal-transfers` — `myinvestor.internal-transfers/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `fecha` | date `AAAA-MM-DD` | no | sí | del cargo (o del abono si no hay cargo) |
| `importe` | number (€, 2 decimales) | no | sí | positivo: lo que pasa de una cuenta a otra |
| `desde_cuenta` | string | sí | sí |  |
| `desde_id` | string | sí | sí | `id` del movimiento de cargo |
| `hacia_cuenta` | string | sí | sí |  |
| `hacia_id` | string | sí | sí | `id` del movimiento de abono |
| `concepto` | string | sí | sí |  |
| `clave` | string | sí | sí | clave leída de `operationRef` |
| `match` | string | sí | sí | `key`, `heuristic` o `null` (sin pareja, con --unmatched) |

### `upcoming` — `myinvestor.upcoming/1`

Salida: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `fecha` | date `AAAA-MM-DD` | no | sí |  |
| `tipo` | string | no | sí | `aportacion`, `orden` o `retencion` |
| `producto` | string | sí | sí |  |
| `importe` | number (€, 2 decimales) | sí | sí | con signo: los cargos son negativos |
| `cuenta_cargo` | string | sí | sí |  |
| `retenido` | boolean | no | sí | una retención lo cubre |
| `disponible` | number (€, 2 decimales) | sí | sí |  |
| `disponible_tras` | number (€, 2 decimales) | sí | sí | disponible tras lo pendiente hasta esa fila |
| `falta` | number (€, 2 decimales) | sí | sí | lo que no cubre la cuenta; `null` si lo cubre |
| `tipo_retencion` | string | sí | sí | `type` de la retención |

### `income` — `myinvestor.income/1`

La última fila es el total.

Con `--by month | --by year`: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `periodo` | string | no | sí | `AAAA-MM` (o `AAAA` con `--by year`); `total` en la última fila |
| `intereses` | number (€, 2 decimales) | no | sí |  |
| `dividendos` | number (€, 2 decimales) | no | sí |  |
| `bruto` | number (€, 2 decimales) | no | sí | intereses + dividendos |
| `retenciones` | number (€, 2 decimales) | no | sí | negativas |
| `neto` | number (€, 2 decimales) | no | sí | bruto + retenciones |
| `comisiones` | number (€, 2 decimales) | no | sí |  |
| `movimientos` | integer | no | sí |  |

Con `--by type`: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `categoria` | string | no | sí | `intereses`, `dividendos`, `retencion`, `comision`… o `total` |
| `tipo` | string | sí | sí | `operationType`; `null` en el total |
| `regla` | string | sí | sí | regla que lo ha clasificado; `null` si es desconocido |
| `movimientos` | integer | no | sí |  |
| `importe` | number (€, 2 decimales) | no | sí |  |

Con `--by account`: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `cuenta` | string | no | sí | alias de la cuenta; `total` en la última fila |
| `cuenta_id` | string | sí | sí | `null` en el total |
| `intereses` | number (€, 2 decimales) | no | sí |  |
| `dividendos` | number (€, 2 decimales) | no | sí |  |
| `bruto` | number (€, 2 decimales) | no | sí | intereses + dividendos |
| `retenciones` | number (€, 2 decimales) | no | sí | negativas |
| `neto` | number (€, 2 decimales) | no | sí | bruto + retenciones |
| `comisiones` | number (€, 2 decimales) | no | sí |  |
| `movimientos` | integer | no | sí |  |

### `allocation` — `myinvestor.allocation/1`

La última fila es el total (`clave: "total"`).

Con `--by asset`: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `clave` | string | no | sí | clave del grupo; `total` en la última fila |
| `origen` | string | sí | sí | etiquetas de la API que dan la clase |
| `valor` | number (€, 2 decimales) | no | sí |  |
| `peso_pct` | number (exacto) | sí | sí | % del total, a 2 decimales |
| `invertido` | number (€, 2 decimales) | sí | sí |  |
| `ganancia` | number (€, 2 decimales) | sí | sí |  |
| `ter` | number (exacto) | sí | sí | TER medio ponderado por valor de lo que lo tiene, en % |
| `objetivo_pct` | number (exacto) | sí | sí | solo con `--target`; peso objetivo en % |
| `desviacion_pct` | number (exacto) | sí | sí | solo con `--target`; peso actual - objetivo, en puntos |
| `aportar` | number (€, 2 decimales) | sí | sí | solo con `--target`; cuánto aportar para llegar al objetivo sin vender |
| `consultado_en` | datetime ISO 8601 con zona | no | sí | fecha y hora de la consulta (la misma en todas las filas) |

Con `--by geo | --by category`: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `clave` | string | no | sí | clave del grupo; `total` en la última fila |
| `valor` | number (€, 2 decimales) | no | sí |  |
| `peso_pct` | number (exacto) | sí | sí | % del total, a 2 decimales |
| `invertido` | number (€, 2 decimales) | sí | sí |  |
| `ganancia` | number (€, 2 decimales) | sí | sí |  |
| `ter` | number (exacto) | sí | sí | TER medio ponderado por valor de lo que lo tiene, en % |
| `objetivo_pct` | number (exacto) | sí | sí | solo con `--target`; peso objetivo en % |
| `desviacion_pct` | number (exacto) | sí | sí | solo con `--target`; peso actual - objetivo, en puntos |
| `aportar` | number (€, 2 decimales) | sí | sí | solo con `--target`; cuánto aportar para llegar al objetivo sin vender |
| `consultado_en` | datetime ISO 8601 con zona | no | sí | fecha y hora de la consulta (la misma en todas las filas) |

Con `--by product`: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `clave` | string | no | sí | clave del grupo; `total` en la última fila |
| `nombre` | string | sí | sí |  |
| `valor` | number (€, 2 decimales) | no | sí |  |
| `peso_pct` | number (exacto) | sí | sí | % del total, a 2 decimales |
| `invertido` | number (€, 2 decimales) | sí | sí |  |
| `ganancia` | number (€, 2 decimales) | sí | sí |  |
| `ter` | number (exacto) | sí | sí | TER medio ponderado por valor de lo que lo tiene, en % |
| `objetivo_pct` | number (exacto) | sí | sí | solo con `--target`; peso objetivo en % |
| `desviacion_pct` | number (exacto) | sí | sí | solo con `--target`; peso actual - objetivo, en puntos |
| `aportar` | number (€, 2 decimales) | sí | sí | solo con `--target`; cuánto aportar para llegar al objetivo sin vender |
| `consultado_en` | datetime ISO 8601 con zona | no | sí | fecha y hora de la consulta (la misma en todas las filas) |

Con `--by account`: lista de objetos.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `clave` | string | no | sí | clave del grupo; `total` en la última fila |
| `cuenta_id` | string | sí | sí |  |
| `tipo` | string | sí | sí | tipo de cuenta |
| `valor` | number (€, 2 decimales) | no | sí |  |
| `peso_pct` | number (exacto) | sí | sí | % del total, a 2 decimales |
| `invertido` | number (€, 2 decimales) | sí | sí |  |
| `ganancia` | number (€, 2 decimales) | sí | sí |  |
| `ter` | number (exacto) | sí | sí | TER medio ponderado por valor de lo que lo tiene, en % |
| `objetivo_pct` | number (exacto) | sí | sí | solo con `--target`; peso objetivo en % |
| `desviacion_pct` | number (exacto) | sí | sí | solo con `--target`; peso actual - objetivo, en puntos |
| `aportar` | number (€, 2 decimales) | sí | sí | solo con `--target`; cuánto aportar para llegar al objetivo sin vender |
| `consultado_en` | datetime ISO 8601 con zona | no | sí | fecha y hora de la consulta (la misma en todas las filas) |

### `status` — `myinvestor.status/1`

Nunca incluye tokens ni la contraseña.

Salida: un objeto.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `logged_in` | boolean | no | sí | activa o renovable sin `login` manual |
| `name` | string | sí | sí |  |
| `customer_id_masked` | string | sí | sí | `***678Z` |
| `device_id` | string | sí | sí |  |
| `store` | string | no | sí | `keyring`, `file` o `memory` |
| `store_path` | string | sí | sí |  |
| `access_expires_at` | datetime ISO 8601 con zona | sí | sí |  |
| `refresh_expires_at` | datetime ISO 8601 con zona | sí | sí |  |
| `password_remembered` | boolean | no | sí |  |
| `can_refresh` | boolean | no | sí |  |
| `can_relogin` | boolean | no | sí |  |
| `relogin_blocked_until` | datetime ISO 8601 con zona | sí | sí |  |
| `last_relogin_error` | string | sí | sí |  |
| `verified_online` | boolean | no | sí | `true` solo si MyInvestor ha respondido |

### `doctor` — `myinvestor.doctor/1`

Salida: un objeto.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `ok` | boolean | no | sí | ninguna comprobación en `FAIL` |
| `cli_version` | string | no | sí |  |
| `checks` | array de objetos | no | sí |  |
| `checks[].check` | string | no | sí | `store`, `sesion`, `red`, `api`, `reloj`, `version`, `mantenimiento` |
| `checks[].status` | string | no | sí | `OK`, `WARN` o `FAIL` |
| `checks[].message` | string | no | no (informativo) | texto para personas |
| `checks[].data` | object | no | no (informativo) | detalles de cada comprobación (latencia, desfase…) |

### `state show` — `myinvestor.state-show/1`

Salida: un objeto.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `path` | string | no | sí | ruta del fichero de estado |
| `version` | integer | no | sí | versión del formato del fichero |
| `movements` | object | no | sí | `{cuenta_uuid: {last_day, seen}}`; formato del fichero de estado (cambia con `version`) |
| `orders` | object | no | sí | `{ref: estado}`; formato del fichero de estado (cambia con `version`) |
| `pension_orders` | object | no | sí | `{ref: estado}`; formato del fichero de estado (cambia con `version`) |
| `contributions` | object | no | sí | `{id: {estado, proxima}}`; formato del fichero de estado (cambia con `version`) |
| `pending` | object | no | sí | lo mostrado con `--no-mark`, con las mismas claves |

### `raw` — `myinvestor.raw/1`

Respuesta de MyInvestor tal cual (claves en camelCase de la API): fuera del contrato de claves.

Salida: lo que responda MyInvestor.

### `fund` — `myinvestor.raw/1`

Respuesta de MyInvestor tal cual (claves en camelCase de la API): fuera del contrato de claves. Sin `--full`, solo unas claves de la ficha.

Salida: lo que responda MyInvestor.

### `error` — `myinvestor.error/1`

Con `-f json`, en una sola línea de stderr. Nunca va en el envoltorio.

Salida: un objeto.

| Clave | Tipo | `null` | Estable | Notas |
|---|---|---|---|---|
| `error` | string | no | sí | tipo de error en snake_case (`signature_required`, `network`…) |
| `exit_code` | integer | no | sí | el mismo código con el que sale el proceso |
| `message` | string | no | no (informativo) | texto para personas |

<!-- END: generado -->
