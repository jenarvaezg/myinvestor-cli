# myinvestor-cli

Cliente Python y CLI **no oficiales y de solo lectura** para [MyInvestor](https://myinvestor.es), construidos observando las llamadas de su web app (`newapp.myinvestor.es`, versión 3.134.0). No hay API pública: esto puede romperse cuando MyInvestor cambie su backend. El CLI no compra, vende, traspasa ni mueve dinero: no implementa ninguna escritura.

## Instalación

```bash
uv tool install 'myinvestor[browser] @ git+https://github.com/jenarvaezg/myinvestor-cli'
# o, desde el repo:
uv sync --extra browser && uv run myinvestor --help
```

El extra `browser` (Playwright) solo hace falta para `myinvestor login` sin flags. Usa el Chrome instalado; si no hay, `uv run playwright install chromium`.

## Login

La web manda un token de **reCAPTCHA Enterprise** en el login, pero el servidor también acepta el login sin él desde un dispositivo ya conocido (verificado el 2026-11-03). Hay tres formas de entrar:

```bash
myinvestor login                                  # abre un navegador: entras como siempre (usuario, contraseña, SMS)
myinvestor login --direct -u 12345678Z            # por terminal, sin reCAPTCHA (funciona: verificado el 2026-11-03)
echo "$PASS" | myinvestor login --direct -u 12345678Z --password-stdin   # también vale MYINVESTOR_PASSWORD
myinvestor login --direct --remember -u 12345678Z # guarda la contraseña → re-login automático (cron)
myinvestor login --refresh-token-stdin --device-id <_capuid> < token.txt  # importar una sesión de la web
myinvestor status
myinvestor logout [--forget-device]
```

- El **access token dura 15 min y el refresh token 30 min**. Cada petición renueva el par si hace falta, así que la sesión vive mientras la uses al menos cada media hora; si no, hay que repetir el login.
- Con `--remember` (solo con `--direct`), la contraseña se guarda en el Keychain (o en `~/.config/myinvestor/session.json`, 0600, con `--store file`) y, cuando el refresh token ha caducado, el CLI vuelve a entrar solo, como la app de Fintonic con el PIN. Se guarda solo si MyInvestor la acepta y se borra con `logout`. Si el re-login automático falla, depende del `errCode` del rechazo: con `UNAUTHORIZED` (contraseña incorrecta) o `UPDATE_PASSWORD_REQUIRED` **se borra en el acto**, para no bloquear la cuenta repitiéndola en cada ejecución; con cualquier otro código, o sin él (reCAPTCHA obligatorio, demasiados intentos, cambios de la API…), **no se borra**, pero el re-login queda en pausa 24 h (o hasta el siguiente `myinvestor login`), así que se intenta como mucho una vez al día. Si lo que falta es un código SMS, falla sin tocar la contraseña. Sin `--remember` la contraseña no se guarda nunca: en el almacén solo van los tokens y el id de dispositivo.
- El navegador usa un perfil propio (`~/.local/share/myinvestor/browser`) para que MyInvestor reconozca el dispositivo y solo pida el SMS la primera vez. Al terminar se borran los tokens de ese perfil: la sesión pasa a ser del CLI.
- `--refresh-token-stdin`: el token está en el localStorage de la web (`myinvestor-app:REFRESH_TOKEN`) y el id de dispositivo en `_capuid`. Cuidado: es una credencial.

## Uso

```bash
myinvestor position                               # efectivo + cuentas de valores + planes, con total
myinvestor accounts [--owner me|junior|other]     # cuentas de efectivo: tipo, tu rol, titular, disponible, retenido, saldo
myinvestor movements [-a "Mi cuenta"] [--from 2026-10-01 --to 2026-10-31 | --days 60] [-s nómina] [--oldest-first] [--owner me|junior|other]
myinvestor movements --id <id> [--from … | --days …]   # un solo movimiento, con todos sus campos
myinvestor holds                                  # saldo retenido (p. ej. por órdenes en curso)
myinvestor balance-history [--from 2026-01-01]    # saldo diario total del efectivo

myinvestor holdings [-a 123456] [--kind fondos|broker]   # posiciones: participaciones, coste, VL, valor, rentabilidad
myinvestor orders [--all | --from 2025-01-01 --to …] [--status EXECUTED] [--pension]
myinvestor pensions                               # planes de pensiones
myinvestor history [IE0032126645|N1234] [-a ID] [--period month|three_months|six_months|year]
myinvestor contributions [--all]                  # aportaciones periódicas
myinvestor fund IE0032126645 [--full]             # ficha del fondo (o del plan: `fund N1234`)
myinvestor funds "s&p 500" [--sort ter,asc] [-n 20] [--page 0]
myinvestor feed [-n 20]                           # «Actividad» de la web
myinvestor raw /cperf-server/api/v2/cash-accounts/self [-p clave=valor]   # GET arbitrario
```

### Formatos de salida

Todas las órdenes de listado aceptan `-f table|json|csv|tsv`:

- **`table`** (por defecto): importes en formato español (`-12,50 €`). Las fechas y los importes nunca se cortan ni se parten; los textos largos (conceptos, nombres) se parten en varias líneas para caber en la terminal. Si ni así cabe, la tabla se sale del ancho antes que recortar un dato. Se respeta `COLUMNS`, y si la salida no es una terminal (cron, una tubería, el `!` de Claude Code, un agente) la tabla ocupa lo que necesite en vez de limitarse a 80 columnas. Con `--wide` (o `MYINVESTOR_WIDE=1`) cada fila va en una sola línea: `myinvestor --wide movements | less -S`.
- **`json`**: los importes son **números** JSON, no texto (`"importe": -12.50`), escritos con el valor exacto del `Decimal`, sin pasar por coma flotante. Importes y saldos (`importe`, `saldo`, `disponible`, `retenido`, `valor`, `invertido`, `ganancia`, `total`) van redondeados a 2 decimales; participaciones, VL, coste medio y porcentajes conservan la precisión de la API. `raw` y `fund` también devuelven números. En Python, `json.loads(texto, parse_float=Decimal)` los recupera sin pérdida.
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
| `operacion_ref` | `operationRef` | campo de ancho fijo compartido por el cargo y el abono de una misma transferencia; se exporta sin interpretar |
| `fichero_ref` | `fileRef` | |

Lo que la API no trae sale como `null` (vacío en CSV); el CLI no rellena nada por su cuenta.

**`id`**: no cambia entre ejecuciones ni entre rangos de fechas, así que sirve para saber qué movimientos son nuevos desde la última vez.

- Con `reference` (el caso normal): `{cuenta_uuid}:{reference}`. `reference` es un contador **por cuenta** (se repite entre cuentas), por eso va con el uuid delante.
- Sin `reference`: `{cuenta_uuid}:h` + los 16 primeros caracteres hexadecimales del sha256 de `cuenta_uuid|fecha|valor_fecha|importe|concepto|tipo|operacion_ref` (sin el saldo).
- Si dos movimientos dan el mismo id (idénticos en esos campos), el segundo lleva `#2`, el tercero `#3`… Se numeran por el saldo y el resto de campos, no por la posición en la lista, así que el orden de la respuesta o el rango pedido no los cambia. Limitación: si más tarde se apunta otro movimiento idéntico el mismo día, la numeración de ese grupo puede cambiar; y se asume que la API filtra `dateFrom`/`dateTo` por fecha de operación (un rango incluye o no el día entero).
- `movements --id ID` busca el movimiento en el rango de `--from`/`--to`/`--days` (por defecto, los últimos 30 días): para uno antiguo, pasa un rango que lo incluya.

Las órdenes (`orders`) usan como `ref` el `reference` de MyInvestor, que según lo observado es único (no se repite entre cuentas), y las aportaciones periódicas (`contributions`) su `contributionId` como `id`: ambos sirven tal cual como clave.

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

```bash
export MYINVESTOR_STORE=file                      # ~/.config/myinvestor/session.json (0600)
echo "$PASS" | myinvestor login --direct --remember -u 12345678Z --password-stdin
myinvestor position -f json                        # semanas después: re-login automático si hace falta
```

El primer login desde un dispositivo nuevo pide SMS o aprobación en la app: hazlo una vez en una terminal interactiva con el mismo `--store`.

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
| 13 | Hace falta un segundo factor que no se puede dar sin terminal | `SignatureRequired`, `SignatureError` |
| 14 | Sesión guardada corrupta | `CorruptStoreError`, `CorruptKeyringError` |

Con `-f json` el error también sale en JSON, en una sola línea de stderr: `{"error": "signature_required", "exit_code": 13, "message": "…"}` (`error` es el nombre de la excepción en snake_case sin `Error`; `network` para los errores de red).

### Movimientos antiguos y segundo factor

La consulta de movimientos solo sale directa para los últimos ~90 días (PSD2). Para rangos más antiguos MyInvestor responde `202` con una petición de firma y pide **aprobarla en la app del móvil**. En una terminal interactiva el CLI lo avisa y espera (`--approval-timeout`, 300 s por defecto) y después repite la consulta firmada. Sin terminal (cron, CI, el `!` de Claude Code) falla con un mensaje claro y **no** te manda la notificación, salvo que pases `--approve` (o `MYINVESTOR_APPROVE=1`): `myinvestor --approve movements --from 2025-01-01`. Lo mismo ocurre con el SMS del login `--direct` en un dispositivo nuevo.

### Silencio, depuración y ritmo de peticiones

Opciones globales (van antes de la orden: `myinvestor -q position -f json`):

- `-q`/`--quiet` (o `MYINVESTOR_QUIET=1`): en stderr solo los errores; se silencian los avisos («Rango de más de 90 días…», «Sin resultados», «Sesión iniciada…»). El código de salida sigue indicando si ha ido bien. Pensado para cron: el log diario se queda con el JSON y los fallos.
- `-v`/`--verbose`: una línea por petición en stderr, `GET /ruta 200 (123 ms)`, marcando `[refresh]`, `[login]`, `[firma]` (endpoints de segundo factor) o `[firmada]` (consulta repetida tras aprobarla), y cuándo se renueva la sesión o se hace re-login. **Nunca** se escriben la query, las cabeceras (`authorization`, `x-signature-id`…), los cuerpos de login/refresh, tokens, contraseñas ni códigos; los ids de firma se enmascaran (`/signature/api/v3/signature/*`). Las rutas sí llevan ids de cuenta: revísalo antes de pegarlo en un issue.
- `--min-interval SEG` (o `MYINVESTOR_MIN_INTERVAL`, por defecto 0): espera mínima entre peticiones del mismo proceso, para no despertar al WAF con muchas cuentas.
- `--retry-blocked N` (o `MYINVESTOR_RETRY_BLOCKED`, por defecto 0): si Imperva bloquea una consulta `GET`, espera 30 s, 2 min, 5 min (y 5 min las siguientes) y la repite hasta N veces. Login, refresh, firmas y cualquier `POST` no se repiten nunca.

stdout solo lleva el resultado: con `-f json` y sin datos sale `[]`, con `-f csv` o `-f tsv` sale vacío, y en ninguno se escribe «Sin resultados». Sin terminal en stderr o con `NO_COLOR` no hay colores ni marcado (lo hace rich).

Dentro de una misma orden, las consultas `GET` idénticas (misma ruta y parámetros) se responden de memoria; las firmadas y las `202` no se guardan, y nada se guarda en disco.

Peticiones que hace cada orden (V = cuentas de valores activas, P = cuentas de pensiones, C = cuentas de efectivo elegidas, por defecto todas):

| Orden | Peticiones |
|---|---|
| `accounts`, `balance-history`, `contributions`, `funds`, `feed`, `raw`, `status` | 1 |
| `position` | 3 + V |
| `movements`, `holds` | 1 + C |
| `holdings` | 1 + V (también con `-a`: se leen todas para resolverla) |
| `orders` | 1 + V; con `-a`: 1 + V + las elegidas; `--pension`: 1 + P |
| `pensions` | 1 + P |
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
