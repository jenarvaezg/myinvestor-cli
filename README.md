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
- Con `--remember` (solo con `--direct`), la contraseña se guarda en el Keychain (o en `~/.config/myinvestor/session.json`, 0600, con `--store file`) y, cuando el refresh token ha caducado, el CLI vuelve a entrar solo, como la app de Fintonic con el PIN. Se guarda solo si MyInvestor la acepta, se borra con `logout` y **se borra al primer rechazo**, para no bloquear la cuenta repitiéndola en cada ejecución. Sin `--remember` la contraseña no se guarda nunca: en el almacén solo van los tokens y el id de dispositivo.
- El navegador usa un perfil propio (`~/.local/share/myinvestor/browser`) para que MyInvestor reconozca el dispositivo y solo pida el SMS la primera vez. Al terminar se borran los tokens de ese perfil: la sesión pasa a ser del CLI.
- `--refresh-token-stdin`: el token está en el localStorage de la web (`myinvestor-app:REFRESH_TOKEN`) y el id de dispositivo en `_capuid`. Cuidado: es una credencial.

## Uso

```bash
myinvestor position                               # efectivo + cuentas de valores + planes, con total
myinvestor accounts                               # cuentas de efectivo: disponible, retenido, saldo
myinvestor movements [-a "Mi cuenta"] [--from 2026-10-01 --to 2026-10-31 | --days 60] [-s nómina] [--oldest-first]
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

Todas las órdenes de listado aceptan `-f table|json|csv`. En JSON y CSV los importes van sin formatear (`-12.5`); en la tabla, en formato español (`-12,50 €`).

### Sin escritorio (cron, Linux)

```bash
export MYINVESTOR_STORE=file                      # ~/.config/myinvestor/session.json (0600)
echo "$PASS" | myinvestor login --direct --remember -u 12345678Z --password-stdin
myinvestor position -f json                        # semanas después: re-login automático si hace falta
```

El primer login desde un dispositivo nuevo pide SMS o aprobación en la app: hazlo una vez en una terminal interactiva con el mismo `--store`.

### Movimientos antiguos y segundo factor

La consulta de movimientos solo sale directa para los últimos ~90 días (PSD2). Para rangos más antiguos MyInvestor responde `202` con una petición de firma y pide **aprobarla en la app del móvil**. En una terminal interactiva el CLI lo avisa y espera (`--approval-timeout`, 300 s por defecto) y después repite la consulta firmada. Sin terminal (cron, CI, el `!` de Claude Code) falla con un mensaje claro y **no** te manda la notificación, salvo que pases `--approve` (o `MYINVESTOR_APPROVE=1`): `myinvestor --approve movements --from 2025-01-01`. Lo mismo ocurre con el SMS del login `--direct` en un dispositivo nuevo.

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
- **Reintentos**: `GET` se reintenta 4 veces con backoff exponencial y jitter ante `429/5xx`, timeouts y errores de red, respetando `Retry-After`. Login, refresh y firmas (`POST`) **nunca** se reintentan.
- **Cabeceras como la web**: `authorization: Bearer`, `x-device-id`, `x-myinvestor-app: version=3.134.0,platform=web`, y `origin`/`referer`/`user-agent` de Chrome, porque Imperva está delante.
- **Fechas**: la API manda medianoches ISO (`2026-11-02T00:00:00.000+0000`); se usan como día natural.

## API observada

Base `https://api.myinvestor.es`. Todas las respuestas usan el sobre `{"payload": {"data": …, "pagination"?: …}, "status": {"code", "message", "timestamp"}}`. `app.myinvestor.es` (la API antigua `/myinvestor-server/rest/…`) responde 403 a todo.

### Autenticación (Keycloak `myinvestor-prod`)

| Método | Ruta | Notas |
|---|---|---|
| POST | `/login/api/v2/auth/token` | `{customerId, password}` + `x-device-id`; la web añade `x-recaptcha-token` y `x-recaptcha-action: LOGIN_V2`. → `{accessToken, refreshToken, expiresIn: 900, refreshExpiresIn: 1800}`, o `202` con petición de firma (dispositivo nuevo); se repite con `x-signature-id`. `isPasswordChangeRequired`, `errCode` en errores |
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
| GET | `/cperf-server/api/v2/cash-accounts/self` | `accountId`, `accountUuid`, `alias`, `iban`, `enabledBalance`, `withheldBalance` |
| GET | `/account/api/v3/account/{accountUuid}/flows?dateFrom&dateTo` | `{flowList, iban, startBalance, endBalance}`; más de ~90 días → `202` + firma INAPP |
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
