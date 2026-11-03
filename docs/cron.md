# Uso desatendido: cron y systemd

Cómo montar una rutina diaria fiable (una foto de la posición, los movimientos recientes…) en un Linux sin escritorio. Todo lo que aparece aquí existe en el CLI actual; los detalles de cada opción están en el [README](../README.md).

Resumen:

1. Instala el CLI en una ruta fija.
2. Entra **una vez** con `MYINVESTOR_STORE=file myinvestor login --direct --remember`, en una terminal.
3. Programa un script que llame al CLI con `-q`, `-f json` y `MYINVESTOR_NO_SIGNATURE=1`, dentro de `flock -n`, con `TZ=Europe/Madrid`, y que mire el código de salida.

## Por qué `--direct --remember --store file`

- El access token dura 15 min y el refresh token 30 min. Una ejecución diaria **siempre** encuentra la sesión caducada, así que necesita volver a entrar sola: eso es lo que hace `--remember`, que guarda la contraseña y la usa para el re-login automático (una petición de login más por ejecución).
- `--remember` solo existe con `--direct`, el login por terminal sin reCAPTCHA. A fecha de 2026-11-03 MyInvestor lo acepta desde un dispositivo conocido; si algún día deja de hacerlo, ver [Estado de `--direct`](../README.md#estado-de---direct).
- `--store file` porque en cron y en systemd el llavero del sistema suele no estar disponible (sin sesión gráfica ni D-Bus) o estar bloqueado. Con `auto` (por defecto) tu terminal podría guardar la sesión en el llavero y el cron buscarla en el fichero: sale con 11 («No hay sesión») aunque acabes de entrar. Fija `MYINVESTOR_STORE=file` **en los dos sitios**. Qué se guarda en el fichero y con qué permisos: [Qué guarda cada almacén](../README.md#qué-guarda-cada-almacén). La contraseña va **en claro** en ese JSON (0600).

## Instalación

cron arranca con un `PATH` mínimo (`/usr/bin:/bin`), así que usa rutas absolutas. Tres opciones:

```bash
# 1. Recomendada: una herramienta instalada, sin resolver dependencias en cada ejecución.
#    El extra [browser] no hace falta: --direct no usa navegador.
uv tool install 'myinvestor @ git+https://github.com/jenarvaezg/myinvestor-cli'
uv tool dir --bin                      # dónde ha quedado: normalmente ~/.local/bin/myinvestor

# 2. Desde un clon del repo (actualizas con git pull cuando quieras):
/home/ana/.local/bin/uv run --quiet --project /home/ana/src/myinvestor-cli --frozen myinvestor --version

# 3. uvx, fijando un commit o tag para no cambiar de versión sin darte cuenta:
/home/ana/.local/bin/uvx --from 'git+https://github.com/jenarvaezg/myinvestor-cli@<commit>' myinvestor --version
```

`--frozen` hace que `uv run` use `uv.lock` tal cual, sin actualizarlo. Comprueba la versión con `myinvestor --version` y el entorno con `myinvestor doctor`.

## Primer login (una vez)

Con el **mismo usuario** del sistema que ejecutará el cron y el mismo almacén:

```bash
export MYINVESTOR_STORE=file
# export MYINVESTOR_STORE_PATH=/home/ana/.config/myinvestor/session.json   # si no quieres depender de XDG_CONFIG_HOME
myinvestor login --direct --remember -u 12345678Z      # pide la contraseña sin eco
# o, desde un gestor de contraseñas (nunca con echo "contraseña" en el historial):
#   gestor-de-contraseñas | myinvestor login --direct --remember -u 12345678Z --password-stdin
```

La ruta por defecto es `$XDG_CONFIG_HOME/myinvestor/session.json` (o `~/.config/…`). Si tu shell define `XDG_CONFIG_HOME` y el cron no, cada uno buscará un fichero distinto: en ese caso fija `MYINVESTOR_STORE_PATH` en los dos.

Si MyInvestor pide un segundo factor (dispositivo nuevo o que ha dejado de reconocer):

- **Código SMS**: en una terminal interactiva lo pregunta. Sin terminal (un agente, `ssh -T`), en dos pasos: `myinvestor login --direct --remember --start -u 12345678Z` envía el SMS y sale con 13, y `myinvestor login --direct --code 123456` lo completa (con `--start --remember` la contraseña queda en el login pendiente y `--code` no la vuelve a pedir). También valen `--code-stdin`, `--code-fd` y `MYINVESTOR_OTP`: ver [Código SMS sin terminal](../README.md#código-sms-sin-terminal).
- **Aprobación en la app** (INAPP): en una terminal, o con `myinvestor --approve login --direct --remember …`, que espera a que lo apruebes hasta `--approval-timeout` (300 s por defecto; va antes de la orden: `myinvestor --approve --approval-timeout 600 login --direct …`). `--approve` no sirve para un código SMS.

Dato observado el 2026-11-03, no una garantía: el primer login desde una IP de datacenter (un dispositivo nuevo) no pidió segundo factor. Imperva sí puede bloquear IPs de datacenter (código 12); `myinvestor doctor` lo comprueba.

Comprueba que el cron podrá renovar la sesión sin ti:

```bash
MYINVESTOR_STORE=file myinvestor status -f json --offline
# → "store": "file", "password_remembered": true, "can_relogin": true, "relogin_blocked_until": null
MYINVESTOR_STORE=file myinvestor doctor
```

## Variables de entorno

| Variable | En el cron | Para qué |
|---|---|---|
| `MYINVESTOR_STORE=file` | **sí** | el mismo almacén que el login (ver arriba) |
| `MYINVESTOR_STORE_PATH` | si la usaste en el login | ruta del fichero de sesión |
| `TZ=Europe/Madrid` | **sí** | ver [Zona horaria](#zona-horaria) |
| `MYINVESTOR_NO_SIGNATURE=1` | **recomendada** | no iniciar nunca un segundo factor: ni SMS ni notificaciones al móvil; lo que lo necesite sale con 13 |
| `MYINVESTOR_QUIET=1` (o `-q`) | recomendada | en stderr solo los errores |
| `MYINVESTOR_RETRY_BLOCKED=N` (o `--retry-blocked N`) | opcional | reintentar las consultas `GET` que bloquee Imperva (30 s, 2 min, 5 min…) |
| `MYINVESTOR_MIN_INTERVAL=SEG` (o `--min-interval`) | opcional | espaciar las peticiones si tienes muchas cuentas |
| `MYINVESTOR_CUSTOMER_ID` | no hace falta | solo la usa `login -u`; el DNI ya está en la sesión guardada |
| `MYINVESTOR_PASSWORD` | **nunca** | ver abajo |
| `MYINVESTOR_OTP` | **nunca** | ver abajo |
| `MYINVESTOR_APPROVE` | **nunca** | ver abajo |

Por qué no:

- **`MYINVESTOR_PASSWORD`**: no la necesitas (el re-login usa la guardada con `--remember`) y en el entorno queda a la vista: en el crontab y sus copias, en `systemctl --user show`, en `/proc/<pid>/environ` (tu usuario y root) y en lo que herede cualquier proceso hijo.
- **`MYINVESTOR_OTP`**: con ella, cada re-login que necesite código **envía un SMS** y prueba ese código, que ya será viejo: un SMS por ejecución y un código incorrecto cada vez. Sin ella, el re-login falla con 13 sin mandar nada.
- **`MYINVESTOR_APPROVE`**: con ella, cada consulta que pida aprobación te manda una **notificación al móvil** (a las 3 de la mañana, si ahí cae el cron) y el proceso espera a que la apruebes hasta `--approval-timeout` (300 s por defecto). Además, acostumbrarse a aprobar avisos que no has pedido es justo lo que aprovecha un fraude. En el cron, mejor `MYINVESTOR_NO_SIGNATURE=1`, que garantiza lo contrario.

## Zona horaria

El CLI usa la zona del proceso para «hoy»: el final por defecto de los rangos (`--to`), `--days`, la ventana de 90 días sin firma (`--free-only` recorta a hoy − 89) y la hora de `consultado_en` en `position`/`holdings`. Si el servidor está en UTC, una ejecución a las 00:30 en Madrid cree que todavía es ayer. Pon `TZ=Europe/Madrid` en el entorno del job.

Ojo: en un crontab, `TZ=…` llega al entorno del comando, pero que el **horario** de la línea se interprete en esa zona depende de la implementación de cron (cronie, por ejemplo, admite `CRON_TZ=`). Si no estás seguro, escribe la hora en la zona del sistema o usa el timer de systemd, que admite la zona en `OnCalendar`.

## No solapar ejecuciones: `flock -n`

Lanza cada ejecución dentro de `flock -n` (util-linux): si la anterior sigue en marcha, la nueva sale al momento en vez de esperar. Usa **un solo fichero de cerrojo para todos tus trabajos de myinvestor** (la foto diaria, uno semanal…), para que no se renueve ni se haga re-login a la vez desde dos sitios.

```bash
flock -n -E 75 /home/ana/.cache/myinvestor-cron.lock /home/ana/bin/myinvestor-diario.sh
```

`-E 75` hace que «ya había otra en marcha» salga con 75 y no con 1, para distinguirlo de un error del script. **No** uses como cerrojo el `session.lock` del almacén: el CLI lo bloquea él mismo al escribir la sesión y se quedaría esperando para siempre a que lo suelte el `flock` que lo ha lanzado.

## crontab

`crontab -e` con tu usuario:

```crontab
SHELL=/bin/bash
# las variables del crontab no se expanden: rutas completas
PATH=/home/ana/.local/bin:/usr/local/bin:/usr/bin:/bin
TZ=Europe/Madrid
MYINVESTOR_STORE=file
MYINVESTOR_NO_SIGNATURE=1
MYINVESTOR_QUIET=1

# m  h  dom mon dow  orden
30   8  *   *   *    flock -n -E 75 "$HOME/.cache/myinvestor-cron.lock" "$HOME/bin/myinvestor-diario.sh" >> "$HOME/.local/state/myinvestor/cron.log" 2>&1
```

En la línea de la orden sí se expande `$HOME` (la ejecuta el shell). Crea antes el directorio del log (`mkdir -p ~/.local/state/myinvestor && chmod 700 ~/.local/state/myinvestor`). Si el equipo está apagado a esa hora, cron no la recupera; el timer de systemd con `Persistent=true` sí.

## systemd `--user` timer

`~/.config/systemd/user/myinvestor-diario.service`:

```ini
[Unit]
Description=Foto diaria de MyInvestor

[Service]
Type=oneshot
Environment=TZ=Europe/Madrid MYINVESTOR_STORE=file MYINVESTOR_NO_SIGNATURE=1 MYINVESTOR_QUIET=1
# %h es tu $HOME; comprueba la ruta de flock con `command -v flock`
ExecStart=/usr/bin/flock -n -E 75 %h/.cache/myinvestor-cron.lock %h/bin/myinvestor-diario.sh
TimeoutStartSec=30min
```

`~/.config/systemd/user/myinvestor-diario.timer`:

```ini
[Unit]
Description=Foto diaria de MyInvestor

[Timer]
OnCalendar=*-*-* 08:30 Europe/Madrid
# si el equipo estaba apagado a esa hora, se ejecuta al arrancar
Persistent=true
RandomizedDelaySec=10min

[Install]
WantedBy=timers.target
```

```bash
systemd-analyze calendar '*-*-* 08:30 Europe/Madrid'   # comprueba que tu systemd acepta la zona
systemctl --user daemon-reload
systemctl --user enable --now myinvestor-diario.timer
loginctl enable-linger "$USER"                         # que tus timers corran sin tener sesión abierta
systemctl --user list-timers
journalctl --user -u myinvestor-diario                 # stdout y stderr del script
```

Con `Persistent=true` la ejecución recuperada puede llegar justo al arrancar, antes de que haya red: saldrá con 12 (el script de abajo reintenta). Un servicio de usuario no puede esperar a `network-online.target` del sistema, por eso se reintenta en el script.

## Códigos de salida: qué hacer con cada uno

Con `-f json`, además del código, el error sale en **una línea JSON en stderr**: `{"error": "relogin_blocked", "exit_code": 11, "message": "…"}`. `error` es el nombre de la excepción en snake_case sin `Error`. Excepción: `status` no escribe esa línea; con `-f json` saca su informe en stdout (con `logged_in: false`) y sale con 11.

| Código | `error` | Qué ha pasado | Qué hacer en el cron |
|---|---|---|---|
| 0 | — | OK | nada |
| 1 | `api`, `login_rejected`, `auth`… | otro error (un 4xx, un cambio de la API, un fallo del CLI) | avisar; `myinvestor doctor` para diagnosticar; no reintentar en bucle |
| 2 | `input` | uso incorrecto | revisar el script |
| 10 | `stored_password_rejected`, `invalid_credentials`, `password_change_required` | MyInvestor dice que la contraseña guardada es incorrecta (`UNAUTHORIZED`) o que hay que cambiarla (`UPDATE_PASSWORD_REQUIRED`): **ya se ha borrado**, para no bloquear la cuenta | avisar a la persona: cambiar la contraseña si hace falta y repetir `myinvestor login --direct --remember`. Las siguientes ejecuciones saldrán con 11 |
| 11 | `relogin_blocked`, `session_expired`, `not_logged_in` | no hay sesión, o ha caducado sin forma de renovarla; incluye el re-login **en pausa 24 h** tras un rechazo sin explicación (la contraseña se conserva) | avisar; `myinvestor status -f json --offline` dice `relogin_blocked_until` y `last_relogin_error`. La pausa se levanta sola (se reintenta como mucho una vez al día) o con un `login --direct --remember` manual |
| 12 | `network`, `network_blocked`, `api` (5xx) | red, Imperva o MyInvestor caído | reintentar más tarde (unos minutos, o en la siguiente ejecución); `--retry-blocked` para los bloqueos de Imperva |
| 13 | `signature_required`, `partial_result`, `signature`… | hace falta un segundo factor que no se puede dar sin terminal (sin SMS enviado ni notificación), o `movements`/`income`/`internal-transfers` han devuelto solo una parte | si fallaba el re-login: avisar para hacer `myinvestor login --direct --start` y luego `--code` (o el login en una terminal). Si es `partial_result`: lo de stdout está incompleto; usa `--free-only` en el cron |
| 14 | `corrupt_store`, `corrupt_keyring` | sesión guardada corrupta | avisar; `myinvestor doctor`, borrar el fichero y repetir el login |

Un re-login que pide código y falla con 13 no toca la contraseña ni pausa nada: cada ejecución volverá a intentarlo (un login sin SMS) y saldrá con 13 hasta que lo resuelvas.

## Qué esperar de la sesión

- **Access 15 min, refresh 30 min.** Cada petición renueva el par si hace falta. Entre dos ejecuciones diarias el refresh token ha caducado, así que cada ejecución hace **re-login** con la contraseña guardada (un `POST` de login; desde un dispositivo conocido, sin segundo factor en lo observado). Varias órdenes seguidas en el mismo script comparten la sesión: la primera entra y guarda los tokens, y las demás los leen del almacén.
- **Si el re-login falla**: contraseña incorrecta o cambio obligatorio → se borra y sale con 10; otro rechazo (reCAPTCHA, demasiados intentos, un cambio de la API…) → se conserva, pausa de 24 h y 11; falta un código SMS → 13 sin enviarlo; error de red → 12 sin cambiar nada.
- **Dos procesos a la vez** (el cron y tú a mano): se coordinan por el almacén. Si uno renueva el par antes que el otro, el segundo ve rechazado su refresh token y, en vez de rendirse, lee el almacén y adopta los tokens nuevos del primero (`MyInvestorClient._refreshed_tokens`). Con `--store file` las escrituras son atómicas y van bajo `flock` en `session.lock`. Lo que **no** se coordina es el re-login: dos procesos con la sesión caducada harían dos logins seguidos (gana el último que guarda). Por eso conviene el `flock -n` común a todos los trabajos.
- **Sin `--remember`** (p. ej. una sesión del navegador o de `--refresh-token-stdin`) no hay re-login: la sesión solo sobrevive si se usa al menos cada 30 min. Un timer cada 20 min con `myinvestor -q status` la mantendría viva; no se ha comprobado si MyInvestor impone una duración máxima de sesión.

## Script de ejemplo

`~/bin/myinvestor-diario.sh` (`chmod 700`): guarda en JSON la posición, los movimientos nuevos desde la ejecución anterior y lo que viene en los próximos 14 días (`upcoming`: aportaciones, órdenes en curso, retenciones y si el disponible lo cubre); reintenta los errores de red y avisa según el código de salida. Usa `status --offline`, `position`, `movements --new`, `state mark`, `upcoming` y `doctor`. Ninguna de esas órdenes pide segundo factor (`movements --new` nunca sale de la ventana de 90 días).

```bash
#!/usr/bin/env bash
# Foto diaria de MyInvestor. Sale con 0, o con el primer código de error de myinvestor.
set -uo pipefail                 # sin -e: los códigos de salida se miran a mano
umask 077                        # datos y logs solo para tu usuario

DATA=${DATA:-"$HOME/myinvestor-datos"}
export TZ=${TZ:-Europe/Madrid}
export MYINVESTOR_STORE=${MYINVESTOR_STORE:-file}
export MYINVESTOR_NO_SIGNATURE=1 # nunca un SMS ni una notificación al móvil desde aquí
unset MYINVESTOR_PASSWORD MYINVESTOR_OTP MYINVESTOR_APPROVE

mi() { "$HOME/.local/bin/myinvestor" -q "$@"; }
# o, desde un clon: mi() { "$HOME/.local/bin/uv" run --quiet --project "$HOME/src/myinvestor-cli" --frozen myinvestor -q "$@"; }

log()    { printf '%s %s\n' "$(date '+%F %T %z')" "$*" >&2; }
notify() { log "AVISO: $*"; }    # cámbialo por tu canal: mail, ntfy, un bot…

day=$(date +%F)
first_error=0
mkdir -p "$DATA"

# fetch NOMBRE DESTINO ARGS…: deja stdout en DESTINO solo si sale con 0; reintenta 12 dos veces.
fetch() {
  local name=$1 dest=$2 rc attempt
  shift 2
  for attempt in 1 2 3; do
    mi --retry-blocked 2 "$@" >"$dest.tmp" 2>"$dest.err"
    rc=$?
    [[ $rc -eq 12 && $attempt -lt 3 ]] || break
    log "$name: red, Imperva o MyInvestor no disponible (12); reintento en 5 min"
    sleep 300
  done
  if [[ $rc -eq 0 ]]; then
    mv "$dest.tmp" "$dest"
  else
    rm -f "$dest.tmp"
    log "$name: código $rc: $(tail -n 1 "$dest.err")"   # con -f json, la línea JSON del error
  fi
  rm -f "$dest.err"
  return "$rc"
}

# react NOMBRE CÓDIGO: qué hacer con cada código de salida.
react() {
  local name=$1 rc=$2
  case $rc in
    0) return 0 ;;
    10) notify "$name: MyInvestor ha rechazado la contraseña guardada (ya borrada) o exige cambiarla. Repite 'myinvestor login --direct --remember' en una terminal." ;;
    11) notify "$name: sin sesión renovable (¿re-login en pausa? mira 'myinvestor status --offline'). Repite 'myinvestor login --direct --remember'." ;;
    12) notify "$name: red, Imperva o MyInvestor caído tras los reintentos; lo intentará la próxima ejecución." ;;
    13) notify "$name: MyInvestor pide un segundo factor. En una terminal: 'myinvestor login --direct --start' y luego 'myinvestor login --direct --code <código>'." ;;
    14) notify "$name: sesión guardada corrupta: 'myinvestor doctor', bórrala y repite el login." ;;
    *) notify "$name: error $rc; 'myinvestor doctor' para diagnosticar." ;;
  esac
  [[ $first_error -ne 0 ]] || first_error=$rc
}

# 1. ¿Hay sesión, o forma de renovarla? Sin llamar a MyInvestor.
status=$(mi status -f json --offline 2>/dev/null)
rc=$?
if [[ $rc -ne 0 ]]; then
  log "estado: $status"            # sin tokens ni contraseña; dice si el re-login está en pausa
  react status "$rc"
  exit "$first_error"
fi

# 2. Datos. Si falla la sesión (10, 11, 13, 14), no sigue: el resto fallaría igual.
fetch position "$DATA/posicion-$day.json" position -f json
react position $?
case $first_error in 10 | 11 | 13 | 14) exit "$first_error" ;; esac

# Solo lo que no salió la vez anterior; se marca como visto cuando ya está guardado.
fetch movements "$DATA/movimientos-$(date +%F-%H%M).json" movements --new --no-mark -f json
rc=$?
[[ $rc -ne 0 ]] || mi state mark movements || rc=$?
react movements $rc

fetch upcoming "$DATA/proximo-$day.json" upcoming --days 14 -f json   # sale con 0 aunque falte saldo
react upcoming $?

# 3. Un error sin clasificar: deja el diagnóstico en el log.
[[ $first_error -ne 1 ]] || mi doctor >&2

log "fin: código $first_error"
exit "$first_error"
```

Cada `movimientos-*.json` lleva solo lo que no salió en la ejecución anterior (la primera vez, los últimos 30 días). Con `--no-mark` + `state mark movements` se marca como visto solo cuando ya está guardado: si algo falla por el camino, la siguiente ejecución lo vuelve a sacar; y si la máquina pasa unos días apagada, pide desde el último día consultado (menos 3 de margen), sin salir de la ventana de 90 días (ver [Solo lo nuevo](../README.md#solo-lo-nuevo---new---since-date-y---since-id)). El `id` de cada movimiento es estable, así que los ficheros se pueden juntar sin duplicados (ver [Movimientos: campos e id](../README.md#movimientos-campos-e-id)). `orders --new` y `contributions --new` hacen lo mismo con las órdenes que cambian de estado y las aportaciones que se ejecutan. En `proximo-*.json`, una fila con `falta` distinto de `null` es una aportación que la cuenta de cargo no cubre: el script puede avisar con ella.

## Logs sin secretos y rotación

Lo que escribe el CLI **nunca** incluye la contraseña, los tokens ni los códigos SMS, tampoco con `-v` (que enmascara los ids de firma y no escribe query, cabeceras ni cuerpos de login). Aun así:

- Los datos (stdout) llevan saldos, IBAN y nombres de titulares, y las líneas de `-v` llevan ids de cuenta en las rutas: trata los ficheros de datos y los logs como privados (`umask 077`, fuera de carpetas compartidas o sincronizadas) y revisa lo que pegues en un issue.
- `status -f json` incluye el id de dispositivo y el DNI enmascarado; no es una contraseña, pero tampoco lo publiques.
- No pongas `set -x` en un script que pase una contraseña por stdin, ni la contraseña como argumento de ningún programa (`ps` la muestra a todos los usuarios), ni en el crontab.
- Un `--refresh-token-stdin` es una credencial: no lo dejes en ficheros ni en el historial.

Rotación del log de cron con `logrotate` como usuario normal (`~/.config/logrotate.conf`):

```
/home/ana/.local/state/myinvestor/cron.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    create 0600
}
```

y en el crontab: `0 7 * * 1 /usr/sbin/logrotate -s "$HOME/.local/state/logrotate.status" "$HOME/.config/logrotate.conf"`. Con systemd la salida va al journal, que rota solo (`journalctl --user -u myinvestor-diario --since -7d` para verla). Los ficheros de datos no los borra nadie: si no quieres guardarlos para siempre, `find "$HOME/myinvestor-datos" -name '*.json' -mtime +400 -delete`.
