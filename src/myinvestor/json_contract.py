"""The JSON contract: keys and types of every command's `-f json` output, and their schema version.

This table is the single source of truth: the contract tests check each command's real output
against it, `myinvestor schema ORDEN` turns it into a JSON Schema and `docs/json.md` is generated
from it. Removing or renaming a key, or changing its type or nullability, is incompatible: bump
`version` (`myinvestor.movements/1` → `/2`) and note it in CHANGELOG.md. Adding a key is compatible.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

SCHEMA_PREFIX = "myinvestor."


class JsonType(StrEnum):
    string = "string"
    amount = "amount"  # JSON number in euros, always with 2 decimals
    number = "number"  # JSON number with the API's precision (prices, participations, percentages)
    integer = "integer"
    boolean = "boolean"
    date = "date"  # string `AAAA-MM-DD`
    datetime = "datetime"  # string ISO 8601 with UTC offset
    object = "object"  # free-form object unless the field lists its `fields`
    array = "array"  # list of objects with the field's `fields`
    any = "any"  # any JSON value (the envelope's `data`)


class Shape(StrEnum):
    rows = "rows"  # a JSON array of objects
    object = "object"  # a single JSON object
    any = "any"  # whatever MyInvestor answered: outside the key contract


@dataclass(frozen=True)
class Field:
    name: str
    type: JsonType
    nullable: bool = False
    note: str = ""
    stable: bool = True  # False: the key stays, its content is informative and may change
    only_with: str | None = None  # the option that adds this key (`--ter`); None: always present
    fields: tuple[Field, ...] = ()  # keys of the objects of an `array` (or of a fixed `object`)


@dataclass(frozen=True)
class Variant:
    """One output shape of a command; `label` names the options that produce it ("" for the default)."""

    label: str
    shape: Shape
    fields: tuple[Field, ...] = ()


@dataclass(frozen=True)
class Contract:
    command: str  # as typed: `balance-history`, `state show`
    version: int
    variants: tuple[Variant, ...]
    note: str = ""
    name: str | None = None  # schema name when it is not the command's (`fund` → `raw`)
    enveloped: bool = True  # False: the output already carries its own `schema` (never wrapped)

    @property
    def schema_id(self) -> str:
        return f"{SCHEMA_PREFIX}{self.name or self.command.replace(' ', '-')}/{self.version}"


S, A, N, INT, B = JsonType.string, JsonType.amount, JsonType.number, JsonType.integer, JsonType.boolean
D, DT, OBJ, ARR = JsonType.date, JsonType.datetime, JsonType.object, JsonType.array

CONSULTED = Field("consultado_en", DT, note="fecha y hora de la consulta (la misma en todas las filas)")

MOVEMENT = (
    Field("id", S, note="id estable: `{cuenta_uuid}:{reference}` (o `:h…` sin reference, `#2` si se repite)"),
    Field("fecha", D, note="fecha de operación"),
    Field("valor_fecha", D, True, "fecha valor"),
    Field("cuenta", S, note="alias de la cuenta (o su id si no tiene); puede cambiar"),
    Field("cuenta_id", S),
    Field("cuenta_uuid", S),
    Field("iban", S, True),
    Field("sentido", S, True, "`flowType`: `IN` / `OUT`"),
    Field("tipo", S, True, "`operationType`"),
    Field("clase", S, True, "`operationClass`"),
    Field("concepto", S, True),
    Field("importe", A, note="con signo: los cargos son negativos"),
    Field("saldo", A, True, "saldo de la cuenta tras el movimiento"),
    Field("origen", S, True, "`operationOriginLabel`"),
    Field("destino", S, True, "`operationTargetLabel`"),
    Field("contraparte", S, True, "`destino` en cargos, `origen` en abonos"),
    Field("ref", S, True, "`reference`: contador por cuenta"),
    Field("operacion_ref", S, True, "`operationRef`, sin interpretar"),
    Field("fichero_ref", S, True, "`fileRef`"),
    Field("pareja_id", S, True, "`id` del otro lado de un traspaso entre tus cuentas"),
    Field("traspaso_interno", B),
)

PRODUCT_VALUES = (
    Field("invertido", A, True),
    Field("valor", A, True),
    Field("ganancia", A, True),
    Field("rentab", N, True, "rentabilidad total, en %"),
)

POSITION = (
    Field("producto", S, note="`efectivo`, `valores`, `pensiones` o `total` (última fila)"),
    Field("id", S, note='id de la cuenta; `""` en el total'),
    Field("nombre", S, True, '`""` en el total'),
    Field("valor", A, True),
    Field("invertido", A, True, "`null` en efectivo y en el total"),
    Field("ganancia", A, True, "`null` en efectivo y en el total"),
    Field("rentab", N, True, "en %; `null` en efectivo y en el total"),
    Field("fecha_valor", D, True, "VL más reciente del producto (hoy en efectivo; `null` en el total)"),
    CONSULTED,
)

ACCOUNT = (
    Field("id", S),
    Field("uuid", S),
    Field("alias", S, True),
    Field("iban", S, True),
    Field("tipo", S, True, "`accountType`"),
    Field("rol", S, True, "tu `participantType`"),
    Field("titular", S, True, "nombre del titular principal (el menor en una cuenta junior)"),
    Field("estado", S, True),
    Field("disponible", A),
    Field("retenido", A),
    Field("saldo", A, note="disponible + retenido"),
    Field(
        "titulares",
        ARR,
        note="nunca el DNI",
        fields=(Field("nombre", S, True), Field("rol", S, True, "`participantType`"), Field("yo", B)),
    ),
)

HOLDING = (
    Field("cuenta", S, note="id de la cuenta de valores"),
    Field("tipo", S, True, "`investmentProductType`: `INDEXED_FUND`, `BROKER`…"),
    Field("isin", S, True),
    Field("nombre", S, True),
    Field("participaciones", N, True),
    Field("coste_medio", N, True),
    Field("vl", N, True, "valor liquidativo / precio"),
    Field("fecha_vl", D, True),
    *PRODUCT_VALUES,
    Field("peso", N, True, "% sobre el valor de las posiciones listadas, a 2 decimales"),
    Field("ter", N, True, "TER de la ficha del fondo, en %; `null` en acciones y ETF", only_with="--ter"),
    CONSULTED,
)

ORDER = (
    Field("fecha", D),
    Field("cuenta", S, True, "id de la cuenta de valores o de pensiones"),
    Field("ref", S, note="`reference`: única"),
    Field("producto", S, True, "ISIN, código DGS o ticker"),
    Field("nombre", S, True),
    Field("operacion", S, True, "`operationType`"),
    Field("estado", S, True, "`IN_PROGRESS`, `EXECUTED`…"),
    Field("importe", A, True),
    Field("participaciones", N, True),
)

PENSION = (
    Field("cuenta", S),
    Field("codigo", S, note="código DGS del plan (`N1234`)"),
    Field("nombre", S, True),
    Field("participaciones", N, True),
    Field("coste_medio", N, True),
    Field("vl", N, True),
    Field("fecha_vl", D, True),
    *PRODUCT_VALUES,
)

HISTORY = (
    Field("fecha", D),
    Field("cuenta", S),
    Field("producto", S, note="ISIN o código DGS"),
    Field("valor", A, True),
    Field("invertido", A, True),
    Field("rentab", N, True, "en %"),
    Field("participaciones", N, True),
    Field("coste_medio", N, True),
)

CONTRIBUTION = (
    Field("id", S, note="`contributionId`"),
    Field("estado", S, True),
    Field("producto", S, True, "ISIN"),
    Field("nombre", S, True),
    Field("importe", A),
    Field("frecuencia", S, True, "`ONE_MONTH`…"),
    Field("proxima", D, True, "próxima ejecución"),
    Field("destino", S, True, "`toAccountType`"),
)

FUND_ROW = (
    Field("isin", S),
    Field("nombre", S, True),
    Field("gestora", S, True),
    Field("categoria", S, True),
    Field("ter", N, True, "en %"),
    Field("riesgo", INT, True, "indicador de riesgo"),
    Field("vl", N, True),
    Field("ytd", N, True, "rentabilidad en el año, en %"),
    Field("1a", N, True, "rentabilidad a 1 año, en %"),
)

FEED = (
    Field("fecha", D, True),
    Field("producto", S, True),
    Field("tipo", S, True),
    Field("titulo", S, True),
    Field("detalle", S, True),
)

TRANSFER = (
    Field("fecha", D, note="del cargo (o del abono si no hay cargo)"),
    Field("importe", A, note="positivo: lo que pasa de una cuenta a otra"),
    Field("desde_cuenta", S, True),
    Field("desde_id", S, True, "`id` del movimiento de cargo"),
    Field("hacia_cuenta", S, True),
    Field("hacia_id", S, True, "`id` del movimiento de abono"),
    Field("concepto", S, True),
    Field("clave", S, True, "clave leída de `operationRef`"),
    Field("match", S, True, "`key`, `heuristic` o `null` (sin pareja, con --unmatched)"),
)

UPCOMING = (
    Field("fecha", D),
    Field("tipo", S, note="`aportacion`, `orden` o `retencion`"),
    Field("producto", S, True),
    Field("importe", A, True, "con signo: los cargos son negativos"),
    Field("cuenta_cargo", S, True),
    Field("retenido", B, note="una retención lo cubre"),
    Field("disponible", A, True),
    Field("disponible_tras", A, True, "disponible tras lo pendiente hasta esa fila"),
    Field("falta", A, True, "lo que no cubre la cuenta; `null` si lo cubre"),
    Field("tipo_retencion", S, True, "`type` de la retención"),
)

INCOME_SUMMARY = (
    Field("intereses", A),
    Field("dividendos", A),
    Field("bruto", A, note="intereses + dividendos"),
    Field("retenciones", A, note="negativas"),
    Field("neto", A, note="bruto + retenciones"),
    Field("comisiones", A),
    Field("movimientos", INT),
)

INCOME_TYPE = (
    Field("categoria", S, note="`intereses`, `dividendos`, `retencion`, `comision`… o `total`"),
    Field("tipo", S, True, "`operationType`; `null` en el total"),
    Field("regla", S, True, "regla que lo ha clasificado; `null` si es desconocido"),
    Field("movimientos", INT),
    Field("importe", A),
)

ALLOCATION_TAIL = (
    Field("valor", A),
    Field("peso_pct", N, True, "% del total, a 2 decimales"),
    Field("invertido", A, True),
    Field("ganancia", A, True),
    Field("ter", N, True, "TER medio ponderado por valor de lo que lo tiene, en %"),
    Field("objetivo_pct", N, True, "peso objetivo en %", only_with="--target"),
    Field("desviacion_pct", N, True, "peso actual - objetivo, en puntos", only_with="--target"),
    Field("aportar", A, True, "cuánto aportar para llegar al objetivo sin vender", only_with="--target"),
    CONSULTED,
)
ALLOCATION_KEY = Field("clave", S, note="clave del grupo; `total` en la última fila")

STATUS = (
    Field("logged_in", B, note="activa o renovable sin `login` manual"),
    Field("name", S, True),
    Field("customer_id_masked", S, True, "`***678Z`"),
    Field("device_id", S, True),
    Field("store", S, note="`keyring`, `file` o `memory`"),
    Field("store_path", S, True),
    Field("access_expires_at", DT, True),
    Field("refresh_expires_at", DT, True),
    Field("password_remembered", B),
    Field("can_refresh", B),
    Field("can_relogin", B),
    Field("relogin_blocked_until", DT, True),
    Field("last_relogin_error", S, True),
    Field("verified_online", B, note="`true` solo si MyInvestor ha respondido"),
)

DOCTOR = (
    Field("ok", B, note="ninguna comprobación en `FAIL`"),
    Field("cli_version", S),
    Field(
        "checks",
        ARR,
        fields=(
            Field("check", S, note="`store`, `sesion`, `red`, `api`, `reloj`, `version`, `mantenimiento`"),
            Field("status", S, note="`OK`, `WARN` o `FAIL`"),
            Field("message", S, note="texto para personas", stable=False),
            Field("data", OBJ, note="detalles de cada comprobación (latencia, desfase…)", stable=False),
        ),
    ),
)

STATE_MAP_NOTE = "formato del fichero de estado (cambia con `version`)"
STATE = (
    Field("path", S, note="ruta del fichero de estado"),
    Field("version", INT, note="versión del formato del fichero"),
    Field("movements", OBJ, note=f"`{{cuenta_uuid: {{last_day, seen}}}}`; {STATE_MAP_NOTE}"),
    Field("orders", OBJ, note=f"`{{ref: estado}}`; {STATE_MAP_NOTE}"),
    Field("pension_orders", OBJ, note=f"`{{ref: estado}}`; {STATE_MAP_NOTE}"),
    Field("contributions", OBJ, note=f"`{{id: {{estado, proxima}}}}`; {STATE_MAP_NOTE}"),
    Field("pending", OBJ, note="lo mostrado con `--no-mark`, con las mismas claves"),
)

RAW_NOTE = "Respuesta de MyInvestor tal cual (claves en camelCase de la API): fuera del contrato de claves."


def _rows(*fields: Field, label: str = "") -> Variant:
    return Variant(label, Shape.rows, fields)


def _allocation(label: str, *labels: Field) -> Variant:
    return _rows(ALLOCATION_KEY, *labels, *ALLOCATION_TAIL, label=label)


CONTRACTS: tuple[Contract, ...] = (
    Contract("position", 1, (_rows(*POSITION),), "Una fila por producto y, al final, el total."),
    Contract("accounts", 1, (_rows(*ACCOUNT),)),
    Contract(
        "movements",
        1,
        (_rows(*MOVEMENT), Variant("--id", Shape.object, MOVEMENT)),
        "Con `--id` sale el objeto del movimiento, no una lista. `--new`, `--since-*` y `--search` solo filtran filas.",
    ),
    Contract(
        "holds",
        1,
        (
            _rows(
                Field("cuenta", S, note="alias de la cuenta (o su id si no tiene)"),
                Field("id", S),
                Field("tipo", S, True, "`INVESTMENT_ORDER`…"),
                Field("importe", A),
            ),
        ),
    ),
    Contract("balance-history", 1, (_rows(Field("fecha", D), Field("saldo", A, note="total del efectivo")),)),
    Contract("holdings", 1, (_rows(*HOLDING),)),
    Contract("orders", 1, (_rows(*ORDER),), "Las mismas claves con `--pension` y con `--new`."),
    Contract("pensions", 1, (_rows(*PENSION),)),
    Contract("history", 1, (_rows(*HISTORY),)),
    Contract("contributions", 1, (_rows(*CONTRIBUTION),)),
    Contract("funds", 1, (_rows(*FUND_ROW),)),
    Contract("feed", 1, (_rows(*FEED),)),
    Contract("internal-transfers", 1, (_rows(*TRANSFER),)),
    Contract("upcoming", 1, (_rows(*UPCOMING),)),
    Contract(
        "income",
        1,
        (
            _rows(
                Field("periodo", S, note="`AAAA-MM` (o `AAAA` con `--by year`); `total` en la última fila"),
                *INCOME_SUMMARY,
                label="--by month | --by year",
            ),
            _rows(*INCOME_TYPE, label="--by type"),
            _rows(
                Field("cuenta", S, note="alias de la cuenta; `total` en la última fila"),
                Field("cuenta_id", S, True, "`null` en el total"),
                *INCOME_SUMMARY,
                label="--by account",
            ),
        ),
        "La última fila es el total.",
    ),
    Contract(
        "allocation",
        1,
        (
            _allocation("--by asset", Field("origen", S, True, "etiquetas de la API que dan la clase")),
            _allocation("--by geo | --by category"),
            _allocation("--by product", Field("nombre", S, True)),
            _allocation("--by account", Field("cuenta_id", S, True), Field("tipo", S, True, "tipo de cuenta")),
        ),
        'La última fila es el total (`clave: "total"`).',
    ),
    Contract("status", 1, (Variant("", Shape.object, STATUS),), "Nunca incluye tokens ni la contraseña."),
    Contract("doctor", 1, (Variant("", Shape.object, DOCTOR),)),
    Contract("state show", 1, (Variant("", Shape.object, STATE),)),
    Contract("raw", 1, (Variant("", Shape.any),), RAW_NOTE),
    Contract("fund", 1, (Variant("", Shape.any),), f"{RAW_NOTE} Sin `--full`, solo unas claves de la ficha.", "raw"),
)

ERROR_LINE = Contract(
    "error",
    1,
    (
        Variant(
            "",
            Shape.object,
            (
                Field("error", S, note="tipo de error en snake_case (`signature_required`, `network`…)"),
                Field("exit_code", INT, note="el mismo código con el que sale el proceso"),
                Field("message", S, note="texto para personas", stable=False),
            ),
        ),
    ),
    "Con `-f json`, en una sola línea de stderr. Nunca va en el envoltorio.",
    enveloped=False,
)

ENVELOPE = (
    Field("schema", S, note="`myinvestor.<orden>/<versión>`"),
    Field("cli_version", S, note="versión del CLI que lo ha generado"),
    Field("as_of", DT, note="inicio de la ejecución"),
    Field("data", JsonType.any, note="la salida de siempre de la orden"),
)

BY_COMMAND = {c.command: c for c in CONTRACTS}


def contract_for(command: str) -> Contract:
    """The contract of a command (`state show` or `state-show`), or of the `error` line."""
    if command == ERROR_LINE.command:
        return ERROR_LINE
    contract = BY_COMMAND.get(command) or BY_COMMAND.get(command.replace("-", " "))
    if contract is None:
        raise KeyError(command)
    return contract
