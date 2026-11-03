# Foto diaria: `snapshot` y `summary`

`myinvestor snapshot` escribe en stdout **un solo JSON** con todo lo que una rutina diaria (cron, un agente) necesita leer: la posición, la cartera, los planes, lo pendiente, lo que viene, lo nuevo desde la vez anterior y la variación frente a la foto anterior, separando el dinero aportado del efecto mercado. Hace un solo login/refresh para todo y, si una parte falla, el resto sale igual. `myinvestor summary` enseña lo mismo en tablas.

```bash
myinvestor snapshot                                     # el JSON en stdout
myinvestor -q snapshot --save ~/myinvestor-datos/fotos  # …y además en fotos/AAAA-MM-DD.json, con el delta frente a la última
myinvestor snapshot --previous fotos/2026-11-01.json    # delta frente a una foto concreta
myinvestor snapshot --no-mark … && procesar && myinvestor state mark   # lo nuevo se marca como visto solo al final
myinvestor summary                                      # la foto de ahora, en tablas (no toca el estado de --new)
myinvestor summary --from-file fotos/2026-11-04.json [--previous fotos/2026-10-28.json]   # una guardada, sin conexión
```

## Opciones

| Opción | `snapshot` | `summary` |
|---|---|---|
| `--save DIR` | también escribe `DIR/AAAA-MM-DD.json` (el día de `as_of`); si ya existe el de hoy, lo sustituye. Fichero 0600, escrito en un temporal que se renombra (nunca queda a medias) bajo `flock` en `DIR/.snapshot.lock`; si `DIR` no existe se crea con 0700 | igual |
| `--previous FILE` | foto contra la que se calcula `delta`. Sin ella, con `--save`, la última `DIR/AAAA-MM-DD.json` **anterior a hoy** (la de hoy nunca). Si `FILE` no existe o no es una foto, error de uso (2) antes de pedir nada | igual; con `--from-file`, recalcula el delta de esa foto |
| `--new/--no-new` | incluir `new` (por defecto **sí**) | por defecto **no**, para que mirar el resumen no consuma lo nuevo de la rutina |
| `--no-mark` | lo nuevo queda pendiente hasta `myinvestor state mark` (ver [Solo lo nuevo](../README.md#solo-lo-nuevo---new---since-date-y---since-id)) | igual |
| `--state PATH` | el fichero de estado de `--new` | igual |
| `--from-file FILE` | — | enseña una foto guardada sin llamar a MyInvestor (no se combina con `--save` ni `--new`) |

Lo nuevo se marca **después** de escribir la foto (en `--save` y en stdout): si algo falla antes, no se marca nada.

## Esquema (`myinvestor.snapshot/1`)

Los nombres de las secciones van en inglés, como en el issue; los campos de cada fila son los de la orden equivalente (en español, sin tildes). Los importes son números JSON exactos, redondeados a céntimos como en `-f json` (participaciones, VL y porcentajes conservan su precisión): `json.loads(texto, parse_float=Decimal)` los recupera sin pérdida.

```json
{"schema": "myinvestor.snapshot/1", "as_of": "2026-11-04T09:00:05+01:00", "cli_version": "0.1.0",
 "total": 12345.67,
 "cash": [{"id": "101", "uuid": "…", "alias": "Mi cuenta", "iban": "…", "tipo": "CASH_ACCOUNT", "rol": "OWNER",
           "titular": "…", "estado": "ACTIVE", "disponible": 950.00, "retenido": 50.00, "saldo": 1000.00,
           "titulares": [{"nombre": "…", "rol": "OWNER", "yo": true}], "fecha_valor": "2026-11-04"}],
 "securities": [{"producto": "valores", "id": "201", "nombre": "…", "valor": 1100.55, "invertido": 1000.00,
                 "ganancia": 100.55, "rentab": 10.06, "fecha_valor": "2026-11-02",
                 "posiciones": [{"cuenta": "201", "tipo": "INDEXED_FUND", "isin": "…", "nombre": "…",
                                 "participaciones": 12.345678, "coste_medio": 81.0, "vl": 89.15, "fecha_vl": "2026-11-02",
                                 "invertido": 1000.00, "valor": 1100.55, "ganancia": 100.55, "rentab": 10.06, "peso": 100}]}],
 "pensions": [{"producto": "pensiones", "id": "301", "nombre": "…", "valor": 2000.00, "invertido": 1800.00, …,
               "planes": [{"cuenta": "301", "codigo": "N1234", "nombre": "…", "participaciones": …, …}]}],
 "pending": {"orders": [{"fecha": "…", "cuenta": "201", "ref": "…", "producto": "…", "nombre": "…",
                         "operacion": "…", "estado": "IN_PROGRESS", "importe": 150.00, "participaciones": null}],
             "holds": [{"cuenta": "Mi cuenta", "id": "…", "tipo": "INVESTMENT_ORDER", "importe": 150.00}]},
 "upcoming": [{"fecha": "…", "tipo": "aportacion", "producto": "…", "importe": -60.00, "cuenta_cargo": "Mi cuenta",
               "retenido": false, "disponible": 950.00, "disponible_tras": 890.00, "falta": null, "tipo_retencion": null}],
 "movements": {"desde": "2026-11-01", "hasta": "2026-11-04", "huecos": [], "movimientos": [ … ]},
 "new": {"movements": [ … ], "orders": [ … ], "pension_orders": [ … ]},
 "delta": {"since": "2026-11-03T09:00:04+01:00", "total": 12.34, "aportado": 0.00, "mercado": 12.34,
           "by_product": [{"producto": "valores", "id": "201", "nombre": "…", "valor": 5.67, "aportado": 0.00, "mercado": 5.67}]},
 "errors": []}
```

| Clave | Contenido | Equivale a |
|---|---|---|
| `schema` | siempre `"myinvestor.snapshot/1"`; cambiará si cambia el significado de algún campo | |
| `as_of` | fecha y hora de la consulta, ISO 8601 con zona | `consultado_en` de `position` |
| `cli_version` | versión del CLI que la ha hecho | `myinvestor --version` |
| `total` | `saldo` de las cuentas de efectivo + `valor` de las de valores y de pensiones; `null` si falla alguna de esas tres secciones | la fila total de `position` |
| `cash` | una fila por cuenta de efectivo, con `titulares` y `fecha_valor` (hoy: el saldo es en tiempo real) | `accounts -f json` |
| `securities` | una fila por cuenta de valores (como en `position`) con sus `posiciones`; `peso` es sobre **toda** la cartera de valores, como en `holdings` sin filtros | `position` + `holdings` |
| `pensions` | una fila por cuenta de pensiones (como en `position`) con sus `planes` | `position` + `pensions` |
| `pending.orders` | órdenes de valores y de planes en curso (`INITIAL`, `PENDING`, `IN_PROGRESS`), de la más reciente a la más antigua | `orders` / `orders --pension` |
| `pending.holds` | retenciones de saldo de cada cuenta de efectivo | `holds` |
| `upcoming` | lo que viene en los próximos **14 días** | `upcoming -f json` |
| `movements` | movimientos de efectivo desde el día de la foto anterior menos 3 días (sin anterior, los últimos 3 días) hasta hoy, de todas las cuentas, con los campos de `movements -f json` (`pareja_id`, `traspaso_interno`…); `huecos`, los tramos que no se han podido pedir. Es la base del `aportado` del efectivo en la foto siguiente | `movements --from … -f json` |
| `new` | `null` con `--no-new`; si no, lo que `movements --new`, `orders --new` y `orders --pension --new` darían, con el mismo estado local | ver [Solo lo nuevo](../README.md#solo-lo-nuevo---new---since-date-y---since-id) |
| `delta` | `null` sin foto anterior; ver abajo | |
| `errors` | `[{"section", "error", "exit_code", "message"}]`, ver abajo | |

Una sección que falla vale `null` (y una lista vacía es que no hay nada): no las confundas. Los movimientos nunca se piden fuera de la ventana de 90 días sin segundo factor.

## `delta`: aportado y mercado

Por producto (cada cuenta de efectivo, de valores y de pensiones, emparejadas por `id` entre las dos fotos; la que solo está en una vale 0 en la otra):

```
valor    = valor ahora − valor antes            (efectivo: saldo)
aportado = valores y pensiones: invertido ahora − invertido antes
           efectivo: Σ importe de los movimientos de la cuenta posteriores a la foto anterior,
                     salvo intereses, dividendos, retenciones y comisiones
mercado  = valor − aportado
```

Y en total: `total` = `total` ahora − `total` antes, `aportado` = Σ `aportado` de los productos, `mercado` = `total` − `aportado`.

- **Qué movimientos son posteriores a la foto anterior.** Los movimientos solo traen el día, no la hora, así que se usan los `id` estables: cuenta un movimiento de `movements` cuyo `id` **no** estaba en el `movements` de la foto anterior y cuya fecha no es anterior al `desde` de esa foto. Así una transferencia que llega por la tarde del mismo día de la foto anterior, o una que aparece con unos días de retraso (hasta 3), cuenta una sola vez.
- **Intereses, dividendos, retenciones y comisiones** (las categorías de [`income`](../README.md#ingresos-intereses-dividendos-retenciones-y-comisiones)) no son dinero aportado sino rendimiento: van a `mercado`. El resto de movimientos del efectivo (transferencias, compras y ventas de valores, aportaciones a planes, pagos con tarjeta, tipos desconocidos) son `aportado`, con su signo.
- **Movimientos entre tus productos.** Un traspaso interno emparejado, una compra de un fondo con el efectivo o una aportación a un plan salen de un producto y entran en otro: cada producto los ve como aportado (negativo en uno, positivo en el otro) y en el total se anulan. Por eso el `aportado` total es el dinero que ha entrado (o salido) de MyInvestor, que es lo que se quiere leer cada mañana, y el `mercado` de cada cuenta de efectivo son sus intereses y comisiones.
- `null` cuando no se puede calcular: si una de las dos fotos no tiene la sección (`cash`, `securities`, `pensions` o `movements`) o la cuenta tiene un hueco en `movements`, o si el `movements` de ahora no llega hasta el día de la foto anterior (la anterior es de hace más de 89 días). El total solo se calcula si están todos los productos.

**Límites** (lo que la fórmula no sabe ver):

- **Ventas.** Al vender, `invertido` baja por el **coste** de lo vendido, mientras que el efectivo recibe el **importe** de la venta. Ese día la ganancia realizada sale como `aportado` (positivo en el total) y como `mercado` negativo en el producto vendido, aunque nada haya entrado de fuera.
- **Dividendos y cupones** cuentan como `mercado` solo si su tipo de movimiento se reconoce como dividendo (o interés); uno con un tipo desconocido (`income --by type` los lista) cuenta como `aportado`.
- **Dinero en tránsito.** Si el cargo de una compra aparece en el efectivo un día y la posición (`invertido`) al siguiente, el total baja ese día con `aportado` negativo y sube al siguiente con `aportado` positivo: el `mercado` sale bien los dos días.
- **Traspasos de fondos o planes desde otra entidad** no pasan por el efectivo: suben `invertido` y salen como `aportado`, que es correcto.
- **Fechas de valor.** Los fondos van con el VL de uno o dos días hábiles antes (`fecha_valor`, `fecha_vl`): el `mercado` de hoy puede ser el movimiento del mercado de anteayer, y un lunes puede no reflejar ningún cambio.
- Si la foto anterior no está completa (p. ej. le falló `pensions`), los productos de esa sección no salen en `by_product` y el total es `null`.

## Errores por sección y código de salida

Todo va con un solo cliente: un login/refresh y las mismas consultas compartidas (la lista de cuentas de efectivo se pide una vez). Si una petición falla, fallan las secciones que la necesitan y el resto sale igual:

| `section` | Lo que necesita |
|---|---|
| `cash` | cuentas de efectivo |
| `securities` | cuentas de valores |
| `pensions` | cuentas y planes de pensiones |
| `pending` | órdenes de valores y de planes, retenciones |
| `upcoming` | cuentas de efectivo, retenciones, aportaciones periódicas, órdenes |
| `movements` | cuentas de efectivo y sus movimientos (un tramo que pide firma deja un hueco y un error `partial_result`, 13; lo que sí se ha obtenido sale) |
| `new.movements`, `new.orders`, `new.pension_orders` | el estado de `--new` (si está corrupto, fallan las tres con 1) y lo mismo que `movements` u órdenes |
| `delta` | la foto anterior encontrada con `--save` (si no es válida: `invalid_snapshot`, 2) |

Cada error es `{"section", "error", "exit_code", "message"}`, con `error` y `exit_code` como en la línea JSON de error del resto de órdenes (ver [Códigos de salida](../README.md#códigos-de-salida)). La foto sale (y se guarda con `--save`) igualmente, y después el CLI sale con **el peor código de las secciones**, en este orden: 13, 1, 2, 12 (de lo que necesita a una persona a lo que se arregla reintentando; 10, 11 y 14 no llegan a una sección, ver abajo). En stderr va además una línea JSON `{"error": "incomplete_snapshot", "exit_code": …, "message": "…"}`. Sin errores sale con 0.

**Lo que aborta todo.** Una sesión inservible (contraseña rechazada 10, sin sesión o re-login en pausa 11, almacén corrupto 14) hace fallar cualquier petición: el CLI sale con ese código sin escribir nada, como cualquier otra orden. La sesión se renueva **una vez**, antes de pedir nada, así que un re-login que necesita un código SMS sale con 13 sin escribir nada en vez de intentarlo en cada sección.

**Segundo factor.** La foto nunca inicia una firma salvo en una terminal interactiva o con `--approve`, y nunca con `--no-signature`: como los movimientos se piden dentro de la ventana de 90 días, en la práctica no pide ninguna. Si MyInvestor la pidiera igualmente, `movements` queda con un hueco y la foto sale con 13.

`summary` enseña las tablas de lo que haya, los errores en stderr y sale con el mismo código; con `--from-file`, enseña los `errors` guardados y sale con 0.

## Peticiones

`snapshot` y `summary`: 4 + 2·C + 2·V + 2·P (C cuentas de efectivo, V cuentas de valores, P cuentas de pensiones); `--from-file`: ninguna. Con `--new`, una cuenta cuyo estado empiece antes que la ventana del delta se pide desde ahí (sigue siendo una petición por cuenta).
