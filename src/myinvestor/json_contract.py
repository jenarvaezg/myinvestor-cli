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

# only `movements --details`: `snapshot` never asks for the sent transfers behind its movements
MOVEMENT_DETAILS = (
    Field(
        "beneficiario",
        S,
        True,
        "`nameDestination` de la transferencia emitida que es este cargo",
        only_with="--details",
    ),
    Field("iban_destino", S, True, "`ibanDestination` de esa transferencia", only_with="--details"),
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
    Field("nombre", S, True, '`""` en el total; en una cartera del roboadvisor, `Cartera METAL (muy agresiva)`'),
    Field("valor", A, True, "en una cartera, sus fondos + el saldo de su cuenta de efectivo propia (`efectivo`)"),
    Field("invertido", A, True, "`null` en efectivo y en el total"),
    Field("ganancia", A, True, "`null` en efectivo y en el total"),
    Field("rentab", N, True, "en %; `null` en efectivo y en el total"),
    Field("fecha_valor", D, True, "VL más reciente del producto (hoy en efectivo; `null` en el total)"),
    Field("cartera", S, True, "`portfolioName` de una cartera del roboadvisor (`METAL`); `null` en el resto"),
    Field("perfil", S, True, "`portfolioProfileType` de la cartera (`VERY_AGGRESSIVE`…); `null` en el resto"),
    Field("efectivo", A, True, "saldo de la cuenta de efectivo propia de la cartera, ya sumado en `valor`"),
    Field("efectivo_id", S, True, "id de esa cuenta de efectivo, que ya no sale como fila aparte"),
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

# Only `accounts` (one more request per account), not the `cash` section of `snapshot`.
ACCOUNT_RATES = (
    Field("tipo_pct", N, True, "`remunerationPercentage` de la última liquidación, en %; null sin remuneración"),
    Field("tae_pct", N, True, "`calculateTaeAverage`, en %; null sin remuneración o si llega 0 (sin calcular)"),
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
    Field("cartera", S, True, "`portfolioName` si la cuenta es una cartera del roboadvisor (`METAL`); si no, `null`"),
    Field("perfil", S, True, "`portfolioProfileType` de esa cartera (`VERY_AGGRESSIVE`…); si no, `null`"),
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
    Field("moneda", S, True, "divisa del fondo (`currency`): la de `vl`"),
)

FUND_FILTER = (
    Field("filtro", S, note="`categories`, `managers`, `currencies`, `asset-types` o `regions`"),
    Field("opcion", S, note="la opción de `funds` (o `pension-plans`) que lo usa: `--category`…"),
    Field("nombre", S, note="el valor que se pasa a la opción, sin espacios ni tabuladores alrededor"),
)

PENSION_PLAN_ROW = (
    Field("dgs", S, True, "código DGS del plan (`N1234`)"),
    Field("nombre", S, True),
    Field("gestora", S, True),
    Field("categoria", S, True),
    Field("ter", N, True, "en %"),
    Field("ytd", N, True, "rentabilidad en el año, en %"),
    Field("1a", N, True, "rentabilidad anualizada a 1 año, en %"),
    Field("3a", N, True, "rentabilidad anualizada a 3 años, en %"),
    Field("5a", N, True, "rentabilidad anualizada a 5 años, en %"),
)

PORTFOLIO_ROW = (
    Field("id", INT, note="id de la cartera en el catálogo"),
    Field("nombre", S, note="`name`: `METAL`… (el `portfolioName` de una cartera contratada)"),
    Field("perfil", S, True, "`CONSERVATIVE`, `MODERATE`, `DYNAMIC`, `AGGRESSIVE` o `VERY_AGGRESSIVE`"),
    Field("renta_fija_pct", N, True, "`fixedIncome`, en %"),
    Field("renta_variable_pct", N, True, "`variableIncome`, en %"),
    Field(
        "rentab", N, True, "`profitability`: % acumulado (no anual) entre `rentab_desde` y `rentab_hasta`, unos 3 años"
    ),
    Field("rentab_desde", D, True, "`profitabilityFrom` (llega como DD/MM/AAAA); `null` si no se puede leer"),
    Field("rentab_hasta", D, True, "`profitabilityTo`"),
    Field("horizonte_anos", INT, True, "`recommendedTimeHorizon`: horizonte recomendado, en años"),
    Field("fondos", INT, note="número de fondos de la composición objetivo"),
)
PORTFOLIO_FUND = (
    Field("isin", S),
    Field("nombre", S, True),
    Field("categoria", S, True),
    Field("categoria_myinvestor", S, True, "`myInvestorCategory`"),
    Field("moneda", S, True),
    Field("peso", N, True, "`percentage`: peso objetivo en la cartera, en %"),
)
PORTFOLIO_DRIFT = (
    Field("isin", S),
    Field("nombre", S, True),
    Field("objetivo_pct", N, True, "peso en el catálogo (0 si el fondo no está en él); `null` si la cartera no está"),
    Field("real_pct", N, True, "% sobre el valor de los fondos de la cartera (sin su efectivo); `null` si está vacía"),
    Field("desviacion_pct", N, True, "`real_pct - objetivo_pct`, en puntos"),
    Field("valor", A, note="valor de mercado de la posición (0 si no se tiene)"),
)
ANNUAL_RETURN = (
    Field("periodo", S, note="año, `AAAA`"),
    Field("importe", A, True, "`amount`"),
    Field("rentab", N, True, "`percentage`, tal cual"),
)
MY_PORTFOLIO = (
    Field("cuenta", S, note="id de la cuenta de valores (`SECURITIES_PORTFOLIO`)"),
    Field("cartera", S, True, "`portfolioName`"),
    Field("perfil", S, True, "`portfolioProfileType`"),
    Field("valor", A, True, "valor de mercado de sus fondos (`marketValue`)"),
    Field("efectivo", A, True, "saldo de su cuenta de efectivo propia (`cashAccountId`)"),
    Field("posiciones", ARR, note="objetivo frente a real, por ISIN", fields=PORTFOLIO_DRIFT),
    Field(
        "rentabilidad_anual",
        ARR,
        note="`years` de `…/v3/portfolios/{id}/returns/summary` (últimos 3; forma sin verificar con datos reales)",
        stable=False,
        fields=ANNUAL_RETURN,
    ),
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

DOCUMENT = (
    Field("id", S, note="`documentUuid`: lo que admite `documents get`"),
    Field("tipo", S, note="lista de origen: `fiscal`, `cash`, `securities`, `pension` o `portfolio`"),
    Field("nombre", S, note="nombre que da MyInvestor (puede acabar en `.pdf` o no)"),
    Field("fecha", D, True, "fecha de creación"),
    Field("cuenta", S, True, "alias, código o id de la cuenta; `null` si el documento no es de una cuenta"),
    Field("notificado", B, True),
)

SENT_TRANSFER = (
    Field("fecha", D, True, "`emissionDate`; `null` en una periódica que aún no se ha emitido"),
    Field("cuenta", S, note="alias de la cuenta de cargo (o su id si no tiene)"),
    Field("cuenta_id", S),
    Field("importe", A, note="negativo, como el cargo"),
    Field("concepto", S, True),
    Field("beneficiario", S, True, "`nameDestination`"),
    Field("iban_destino", S, True, "`ibanDestination`, sin espacios"),
    Field("banco", S, True, "`bank`"),
    Field("bic", S, True, "`bicSwift`"),
    Field("tipo", S, True, "`transferType` (visto: `PUNCTUAL`)"),
    Field("ejecucion", S, True, "`executionType`: `STANDARD` o `INSTANT`"),
    Field("estado", S, True, "`status`: `COMMITED` (sic), `ACTIVE`, `PENDING`, `CANCELED`…"),
    Field("ref", S, note="`reference`: el id de operación del `operationRef` del cargo"),
    Field("periodicidad", S, True, "`periodicity` de las periódicas (`MONTHLY`…)"),
    Field("inicio", D, True, "`startDate`"),
    Field("fin", D, True, "`endDate`"),
    Field("proxima", D, True, "próxima ejecución de una programada (estimada, sin festivos)"),
)

INTEREST = (
    Field("cuenta", S, note="alias de la cuenta (o su id si no tiene)"),
    Field("cuenta_id", S),
    Field("periodo", S, True, "mes liquidado, `AAAA-MM` (`remunerationDate`)"),
    Field("fecha_pago", D, True, "`paymentDate`: día del abono"),
    Field("fecha_oficial", D, True, "`officialRemunerationDate`"),
    Field("saldo_base", A, True, "`applicableBaseBalance`"),
    Field("saldo_promo", A, True, "`balancePromo`"),
    Field("saldo_total", A, True, "`totalBalance`"),
    Field("tipo_pct", N, True, "`remunerationPercentage`, en %"),
    Field("tipo_base_pct", N, True, "`baseRemunerationPercentage`, en %"),
    Field("tipo_promo_pct", N, True, "`promotionRemunerationPercentage`, en %"),
    Field("tae_pct", N, True, "`calculateTaeAverage`, en %"),
    Field("tae_promo_pct", N, True, "`taePromotion`, en %"),
    Field("intereses_base", A, True, "`baseResults`"),
    Field("intereses_promo", A, True, "`promoResults`"),
    Field("bruto", A, True, "`finalResult`: intereses brutos del periodo"),
    Field("retencion", A, True, "`taxChargeAmount`, en negativo"),
    Field("neto", A, True, "`bruto` + `retencion`"),
    Field("remuneracion", S, True, "`remunerationType` (visto: `DEFAULT`)"),
    Field("promocion", S, True, "`promotionCode`"),
    Field("fin_promocion", D, True, "`endDateRemuneration`"),
)

UPCOMING = (
    Field("fecha", D),
    Field("tipo", S, note="`aportacion`, `transferencia`, `orden` o `retencion`"),
    Field("producto", S, True),
    Field("importe", A, True, "con signo: los cargos son negativos"),
    Field("cuenta_cargo", S, True),
    Field("retenido", B, note="una retención lo cubre"),
    Field("disponible", A, True),
    Field("disponible_tras", A, True, "disponible tras lo pendiente hasta esa fila"),
    Field("falta", A, True, "lo que no cubre la cuenta; `null` si lo cubre"),
    Field("tipo_retencion", S, True, "`type` de la retención"),
    Field(
        "estado",
        S,
        note="`programada` (aportaciones y transferencias: aún no ha empezado, es lo único que resta en "
        "`disponible_tras`) o `en_curso` (órdenes y retenciones: ya fuera del disponible, también en `orders` "
        "y `holds`)",
    ),
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

HOLD = (
    Field("cuenta", S, note="alias de la cuenta (o su id si no tiene)"),
    Field("id", S),
    Field("tipo", S, True, "`INVESTMENT_ORDER`…"),
    Field("importe", A, note="negativo, como un cargo en `movements`: dinero retenido que sale del disponible"),
)

SNAPSHOT_ACCOUNT_VALUES = (
    Field("producto", S, note="`valores` o `pensiones`"),
    Field("id", S, note="id de la cuenta"),
    Field("nombre", S, True),
    Field("valor", A, True),
    Field("invertido", A, True),
    Field("ganancia", A, True),
    Field("rentab", N, True, "en %"),
    Field("fecha_valor", D, True, "VL más reciente de sus posiciones"),
)
SNAPSHOT_HOLDING = tuple(f for f in HOLDING if f.name not in ("ter", "consultado_en", "cartera", "perfil"))
SNAPSHOT_SECTION = "`null` si la sección ha fallado (ver `errors`)"
DELTA_ROW = (
    Field("producto", S, note="`efectivo`, `valores` o `pensiones`"),
    Field("id", S, note="id de la cuenta"),
    Field("nombre", S, True),
    Field("valor", A, note="valor ahora - valor antes (en efectivo, el saldo)"),
    Field("aportado", A, True, "dinero neto que ha entrado en el producto; `null` si no se puede saber"),
    Field("mercado", A, True, "valor - aportado"),
)
SNAPSHOT = (
    Field("schema", S, note="`myinvestor.snapshot/1`"),
    Field("as_of", DT, note="fecha y hora de la consulta; su día da el nombre del fichero de `--save`"),
    Field("cli_version", S),
    Field("total", A, True, "efectivo + valores + pensiones; `null` si falla alguna de esas secciones"),
    Field("cash", ARR, True, f"como `accounts`; {SNAPSHOT_SECTION}", fields=(*ACCOUNT, Field("fecha_valor", D))),
    Field(
        "securities",
        ARR,
        True,
        SNAPSHOT_SECTION,
        fields=(
            *SNAPSHOT_ACCOUNT_VALUES,
            Field("posiciones", ARR, note="como `holdings`; `peso` sobre toda la cartera", fields=SNAPSHOT_HOLDING),
        ),
    ),
    Field(
        "pensions",
        ARR,
        True,
        SNAPSHOT_SECTION,
        fields=(*SNAPSHOT_ACCOUNT_VALUES, Field("planes", ARR, note="como `pensions`", fields=PENSION)),
    ),
    Field(
        "pending",
        OBJ,
        True,
        SNAPSHOT_SECTION,
        fields=(
            Field("orders", ARR, note="órdenes de valores y de planes en curso", fields=ORDER),
            Field("holds", ARR, note="como `holds`", fields=HOLD),
        ),
    ),
    Field(
        "upcoming",
        ARR,
        True,
        "como `upcoming`, 14 días; las filas `en_curso` son las órdenes y retenciones de `pending`: para no "
        f"contarlas dos veces, suma solo las `programada`; {SNAPSHOT_SECTION}",
        fields=UPCOMING,
    ),
    Field(
        "movements",
        OBJ,
        True,
        f"movimientos recientes, base del delta siguiente; {SNAPSHOT_SECTION}",
        fields=(
            Field("desde", D),
            Field("hasta", D),
            Field(
                "huecos",
                ARR,
                note="tramos que no se han podido pedir",
                fields=(Field("cuenta_id", S), Field("cuenta", S), Field("desde", D), Field("hasta", D)),
            ),
            Field("movimientos", ARR, fields=MOVEMENT),
        ),
    ),
    Field(
        "new",
        OBJ,
        True,
        "`null` con `--no-new`",
        fields=(
            Field("movements", ARR, True, SNAPSHOT_SECTION, fields=MOVEMENT),
            Field("orders", ARR, True, SNAPSHOT_SECTION, fields=ORDER),
            Field("pension_orders", ARR, True, SNAPSHOT_SECTION, fields=ORDER),
        ),
    ),
    Field(
        "delta",
        OBJ,
        True,
        "`null` sin foto anterior",
        fields=(
            Field("desde", DT, note="`as_of` de la foto anterior"),
            Field("total", A, True),
            Field("aportado", A, True),
            Field("mercado", A, True),
            Field("por_producto", ARR, fields=DELTA_ROW),
        ),
    ),
    Field(
        "errors",
        ARR,
        note="secciones que han fallado",
        fields=(
            Field("section", S, note="`cash`, `pensions`, `movements`, `new.orders`…"),
            Field("error", S, note="como `error` de la línea de error"),
            Field("exit_code", INT),
            Field("message", S, note="texto para personas", stable=False),
        ),
    ),
)

ORDER_HEAD = (
    Field("cuenta", S, note="id de la cuenta de valores o de pensiones"),
    Field("tipo_cuenta", S, note="`valores` o `pensiones`"),
    Field("ref", S, note="`orderReference`"),
    Field("fecha", D, True, "fecha de la orden"),
    Field("isin", S, True, "ISIN (código DGS en un plan de pensiones)"),
    Field("fondo", S, True),
    Field("operacion", S, True, "`operationType`: `INVESTMENT_FUNDS_SUBSCRIPTION`, `INVESTMENT_FUND_REIMBURSEMENT`…"),
    Field("tipo", S, True, "`orderTypeName`"),
    Field("estado", S, True, "`INITIAL`, `PENDING`, `IN_PROGRESS`, `COMPLETE`, `CANCEL`, `REJECTED`"),
    Field("importe", A, True, "importe bruto en euros"),
    Field("participaciones", N, True),
    Field("divisa", S, note="divisa del fondo"),
    Field("vencimiento", D, True, "`expirationDate`"),
)

EXECUTION = (
    Field("num_operacion", S, True, "`operationNumber`"),
    Field("fecha", D, True, "fecha de ejecución"),
    Field("participaciones", N, True, "participaciones ejecutadas"),
    Field("vl", N, True, "valor liquidativo aplicado"),
    Field("bruto", A, True, "en la divisa del fondo"),
    Field("comisiones", A, True, "en la divisa del fondo"),
    Field("retenciones", A, True, "en origen + en destino, en la divisa del fondo"),
    Field("neto", A, True, "en euros"),
    Field("tipo_cambio", N, True),
    Field("resultado", A, True, "`taxResult`: resultado fiscal (en ventas y traspasos de salida)"),
)

LOT = (
    Field("cuenta", S, note="id de la cuenta de valores; `total` en la última fila"),
    Field("isin", S, True, "`null` en el total"),
    Field("fondo", S, True),
    Field("fecha_fiscal", D, True, "fecha fiscal del lote: heredada del fondo de origen en un traspaso"),
    Field("operacion", S, True, "`operationDesc`: `SUSCRIPCIÓN FONDOS INVERSIÓN`, `ALTA IIC SWITCH`…"),
    Field("participaciones", N, True, "participaciones vivas del lote"),
    Field("vl_compra", N, True, "VL de compra"),
    Field("coste", A, True, "coste fiscal"),
    Field("valor", A, True, "valor actual"),
    Field("plusvalia", A, True, "plusvalía (o minusvalía) latente"),
)

GAIN = (
    Field("cuenta", S, note="id de la cuenta de valores; `plusvalias`, `minusvalias` y `total` en las tres últimas"),
    Field("fecha", D, True, "fecha de ejecución; `null` en los totales"),
    Field("ref", S, True, "referencia de la orden; `null` en los totales"),
    Field("isin", S, True),
    Field("fondo", S, True),
    Field("operacion", S, True, "`operationType` de la orden"),
    Field("traspaso", B, True, "traspaso entre fondos: no suma en los totales"),
    Field("participaciones", N, True),
    Field("vl", N, True),
    Field("bruto", A, True),
    Field("comisiones", A, True),
    Field("retenciones", A, True),
    Field("neto", A, True),
    Field("resultado", A, True, "`taxResult`; en los totales, la suma de los reembolsos"),
)

FUND_TRANSFER = (
    Field("fecha", D, True, "`transferDate`"),
    Field("cuenta", S, note="id de la cuenta de valores o, con `--pension`, de pensiones"),
    Field("origen", S, True, "`isinOrigin` o, con `--pension`, `dgsCodeOrigin`"),
    Field("origen_nombre", S, True, "`fundNameOrigin` (solo con `--pension`; `null` en los fondos)"),
    Field("destino", S, True, "`isinDestination` o, con `--pension`, `dgsCodeDestination`"),
    Field("destino_nombre", S, True, "`pensionPlanNameDestination` (solo con `--pension`; `null` en los fondos)"),
    Field("participaciones", N, True, "`shares`"),
    Field("importe", A, True, "`amount`, en `moneda`"),
    Field("moneda", S, True, "`currency`"),
    Field("estado", S, True, "`status`, sin interpretar"),
    Field("cancelable", B, note="`isCancelable` (sin él, `false`, como la web)"),
    Field("ref", S, note="`reference`"),
)

FUND_TRANSFER_PENDING = (
    Field("cuenta", S, note="id de la cuenta de valores"),
    Field("pendientes", INT, note="`pendingTransfers`: traspasos de fondos en curso"),
)

RAW_NOTE = "Respuesta de MyInvestor tal cual (claves en camelCase de la API): fuera del contrato de claves."


def _rows(*fields: Field, label: str = "") -> Variant:
    return Variant(label, Shape.rows, fields)


def _allocation(label: str, *labels: Field) -> Variant:
    return _rows(ALLOCATION_KEY, *labels, *ALLOCATION_TAIL, label=label)


CONTRACTS: tuple[Contract, ...] = (
    Contract("position", 1, (_rows(*POSITION),), "Una fila por producto y, al final, el total."),
    Contract("accounts", 1, (_rows(*ACCOUNT, *ACCOUNT_RATES),)),
    Contract(
        "movements",
        1,
        (_rows(*MOVEMENT, *MOVEMENT_DETAILS), Variant("--id", Shape.object, (*MOVEMENT, *MOVEMENT_DETAILS))),
        "Con `--id` sale el objeto del movimiento, no una lista. `--new`, `--since-*` y `--search` solo filtran filas.",
    ),
    Contract("holds", 1, (_rows(*HOLD),)),
    Contract("balance-history", 1, (_rows(Field("fecha", D), Field("saldo", A, note="total del efectivo")),)),
    Contract("holdings", 1, (_rows(*HOLDING),)),
    Contract("orders", 1, (_rows(*ORDER),), "Las mismas claves con `--pension` y con `--new`."),
    Contract("pensions", 1, (_rows(*PENSION),)),
    Contract("history", 1, (_rows(*HISTORY),)),
    Contract("contributions", 1, (_rows(*CONTRIBUTION),)),
    Contract("funds", 1, (_rows(*FUND_ROW),), "Las mismas claves con cualquier filtro."),
    Contract("fund-filters", 1, (_rows(*FUND_FILTER),), "Las mismas claves con un filtro y con `--pension`."),
    Contract("pension-plans", 1, (_rows(*PENSION_PLAN_ROW),), "El catálogo de planes; las mismas claves con filtros."),
    Contract(
        "fund-history",
        1,
        (
            _rows(
                Field("fecha", D),
                Field("vl", N, note="valor liquidativo (`marketValue`), en `moneda`"),
                Field("moneda", S, True, "divisa del fondo, de su ficha; `null` si no se ha podido leer"),
            ),
        ),
        "Más antiguas primero, desde el inicio del fondo. Con `--every week|month`, el último punto de cada "
        "semana o mes. La `variacion_pct` de la tabla no va en JSON/CSV/TSV.",
    ),
    Contract(
        "portfolios",
        1,
        (
            _rows(*PORTFOLIO_ROW),
            Variant(
                "CARTERA",
                Shape.object,
                (
                    Field(
                        "cartera",
                        OBJ,
                        fields=(
                            *PORTFOLIO_ROW,
                            Field("descripcion", S, True, "`description`"),
                            Field("benchmark", S, True, "`codBenchmark`"),
                        ),
                    ),
                    Field("fondos", ARR, note="composición objetivo", fields=PORTFOLIO_FUND),
                ),
            ),
            _rows(*MY_PORTFOLIO, label="--mine"),
        ),
        "Catálogo del roboadvisor; con CARTERA, su composición; con `--mine`, una fila por cartera contratada.",
    ),
    Contract("feed", 1, (_rows(*FEED),)),
    Contract("internal-transfers", 1, (_rows(*TRANSFER),)),
    Contract("upcoming", 1, (_rows(*UPCOMING),)),
    Contract("documents", 1, (_rows(*DOCUMENT),), "Más recientes primero; con `--kind`, las mismas claves."),
    Contract(
        "transfers",
        1,
        (_rows(*SENT_TRANSFER),),
        "Las mismas claves con `--scheduled` y con `--ref`. Nunca incluye el DNI del ordenante.",
    ),
    Contract("interest", 1, (_rows(*INTEREST),), "Una fila por cuenta con remuneración: la última liquidación."),
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
    Contract(
        "order",
        1,
        (
            Variant(
                "",
                Shape.object,
                (
                    Field("orden", OBJ, fields=ORDER_HEAD),
                    Field(
                        "ejecuciones", ARR, note="`relatedOperations`; vacía si no se ha ejecutado", fields=EXECUTION
                    ),
                ),
            ),
        ),
        "Un objeto con la orden y sus ejecuciones. Nunca incluye el DNI del ordenante.",
    ),
    Contract("lots", 1, (_rows(*LOT),), 'Una fila por lote vivo y, al final, el total (`cuenta: "total"`).'),
    Contract(
        "gains",
        1,
        (_rows(*GAIN),),
        "Una fila por ejecución de un reembolso o traspaso de salida y, al final, `plusvalias`, `minusvalias` y "
        "`total` (solo reembolsos).",
    ),
    Contract(
        "fund-transfers",
        1,
        (_rows(*FUND_TRANSFER), _rows(*FUND_TRANSFER_PENDING, label="--pending")),
        "Las mismas claves con `--pension`. Con `--pending`, una fila por cuenta de valores con el contador.",
    ),
    Contract("status", 1, (Variant("", Shape.object, STATUS),), "Nunca incluye tokens ni la contraseña."),
    Contract("doctor", 1, (Variant("", Shape.object, DOCTOR),)),
    Contract("state show", 1, (Variant("", Shape.object, STATE),)),
    Contract(
        "snapshot",
        1,
        (Variant("", Shape.object, SNAPSHOT),),
        "Siempre JSON (sin `-f`), con su propio `schema`: `--json-envelope` no la envuelve. Ver docs/snapshot.md.",
        enveloped=False,
    ),
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
                Field(
                    "error",
                    S,
                    note="tipo de error en snake_case (`signature_required`, `network`, `unexpected_response`…)",
                ),
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
