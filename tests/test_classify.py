"""`classify`: operationType/operationClass → income category."""

from __future__ import annotations

import pytest

from myinvestor import classify as classify_module
from myinvestor.classify import EXACT_TYPE_RULE, PAIRED_RULE, Category, classify, normalize


@pytest.mark.parametrize(
    ("operation_type", "category"),
    [
        ("CARGO POR TRANSFERENCIA", Category.aportacion_externa),
        ("ABONO POR TRANSFERENCIA", Category.aportacion_externa),
        ("TRANSFERENCIA INMEDIATA", Category.aportacion_externa),
        ("TRANSFERENCIA SEPA", Category.aportacion_externa),
        ("COMPRA RV CONTADO", Category.compra),
        ("APERTURA", Category.otros),
        ("LIQUIDAC. INTERESES", Category.intereses),
        ("SUSCRIPCION IIC", Category.compra),
        ("APORTACIÓN PLANES", Category.aportacion_plan),
    ],
)
def test_observed_types_match_exactly(operation_type: str, category: Category) -> None:
    result = classify(operation_type, "XX")
    assert (result.category, result.rule, result.known) == (category, EXACT_TYPE_RULE, True)


@pytest.mark.parametrize(
    ("operation_type", "category"),
    [
        ("LIQUIDACIÓN INTERESES", Category.intereses),
        ("liquidacion  intereses cuenta", Category.intereses),
        ("REMUNERACION CUENTA", Category.intereses),
        ("ABONO DIVIDENDO", Category.dividendos),
        ("RETENCION IRPF INTERESES", Category.retencion),
        ("RETENCIÓN DIVIDENDOS", Category.retencion),
        ("IRPF", Category.retencion),
        ("COMISION CUSTODIA", Category.comision),
        ("COMISIÓN COMPRA RV", Category.comision),
        ("CUSTODIA VALORES", Category.comision),
        ("VENTA RV CONTADO", Category.venta),
        ("SUSCRIPCION FONDOS", Category.compra),
        ("TRASPASO ENTRE CUENTAS", Category.traspaso_interno),
        ("TRANSFERENCIA PERIODICA", Category.aportacion_externa),
    ],
)
def test_keyword_rules(operation_type: str, category: Category) -> None:
    result = classify(operation_type)
    assert result.category is category and result.known and (result.rule or "").startswith("palabra clave")


@pytest.mark.parametrize("operation_type", ["CARGO POR TRANSFERENCIA", "ABONO POR TRANSFERENCIA", None])
def test_a_paired_internal_transfer_is_traspaso_interno(operation_type: str | None) -> None:
    result = classify(operation_type, internal_transfer=True)
    assert (result.category, result.rule) == (Category.traspaso_interno, PAIRED_RULE)
    assert classify(operation_type).category is not Category.traspaso_interno


@pytest.mark.parametrize("operation_type", ["PAGO CON TARJETA", "COMPRA TARJETA", "SERVICIO RVX", "", None])
def test_unknown_types_go_to_otros(operation_type: str | None) -> None:
    result = classify(operation_type)
    assert (result.category, result.known) == (Category.otros, False)


def test_class_table_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(classify_module, "EXACT_CLASSES", {"DV": Category.dividendos})
    assert classify("ALGO NUEVO", "dv").category is Category.dividendos
    assert classify("TRANSFERENCIA SEPA", "DV").category is Category.aportacion_externa


def test_normalize() -> None:
    assert normalize(" Liquidación  intereses ") == "LIQUIDACION INTERESES"
    assert normalize(None) == ""
