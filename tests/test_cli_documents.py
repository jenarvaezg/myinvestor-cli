"""`documents`, `documents get|summary` and `xray` (synthetic data and PDFs)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common, documents
from myinvestor.auth import REFRESH_PATH
from myinvestor.cli import _fail, app, main
from myinvestor.cli_errors import requested
from myinvestor.errors import EXIT_USAGE, InputError
from myinvestor.session import Session
from myinvestor.stores import MemorySessionStore
from tests.conftest import API, CPERF, cash_account, make_jwt, ok, tokens
from tests.test_documents import DOC_UUID, PDF, PDF_HEADERS, SIGNED, document

runner = CliRunner()
OLD_UUID = "dddddddd-0000-0000-0000-000000000002"
CASH_DOC_UUID = "dddddddd-0000-0000-0000-000000000003"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


@pytest.fixture
def lists(mock_api: respx.MockRouter) -> respx.MockRouter:
    mock_api.get(f"{CPERF}/v2/customers/self/documents/fiscal").respond(
        200,
        json=ok(
            [
                document(DOC_UUID, "INFORMACIÓN FISCAL 2025", createDate="2026-03-01T00:00:00.000+0000"),
                document(OLD_UUID, "INFORMACIÓN FISCAL 2024", createDate="2025-03-01T00:00:00.000+0000"),
            ]
        ),
    )
    mock_api.get(f"{CPERF}/v2/cash-accounts/self/documents").respond(
        200,
        json=ok(
            [
                document(
                    CASH_DOC_UUID,
                    "Informe de costes y gastos 2025.pdf",
                    createDate="2026-01-15T00:00:00.000+0000",
                    accountId="101",
                    accountCode="C101",
                    isNotified=True,
                )
            ]
        ),
    )
    for path in ("securities-accounts", "pension-accounts", "portfolios"):
        mock_api.get(f"{CPERF}/v2/{path}/self/documents").respond(200, json=ok([]))
    return mock_api


def invoke(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def run_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    """Through `main`, so errors become exit codes and messages."""
    monkeypatch.setattr("sys.argv", ["myinvestor", *args])
    with pytest.raises(SystemExit) as exited:
        main()
    return int(exited.value.code or 0), capsys.readouterr().err


# ---- list -------------------------------------------------------------------


@pytest.mark.usefixtures("store", "lists")
def test_documents_json_rows_newest_first() -> None:
    result = invoke("documents", "-f", "json")

    rows = json.loads(result.stdout)
    assert [r["id"] for r in rows] == [DOC_UUID, CASH_DOC_UUID, OLD_UUID]
    assert rows[1] == {
        "id": CASH_DOC_UUID,
        "tipo": "cash",
        "nombre": "Informe de costes y gastos 2025.pdf",
        "fecha": "2026-01-15",
        "cuenta": "C101",
        "notificado": True,
    }


@pytest.mark.usefixtures("store")
def test_documents_kind_asks_one_list(lists: respx.MockRouter) -> None:
    result = invoke("documents", "--kind", "cash", "-f", "csv")

    assert result.stdout.splitlines()[0] == "id,tipo,nombre,fecha,cuenta,notificado"
    assert len(result.stdout.splitlines()) == 2
    assert not lists.routes[0].called  # the fiscal list


@pytest.mark.usefixtures("store", "lists")
def test_documents_table() -> None:
    result = invoke("documents")
    assert "INFORMACIÓN FISCAL 2025" in result.stdout


# ---- get ----------------------------------------------------------------------


@pytest.mark.usefixtures("store", "lists")
def test_get_fiscal_year_saves_a_private_pdf(
    mock_api: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_api.get(SIGNED).respond(200, content=PDF, headers=PDF_HEADERS)
    monkeypatch.chdir(tmp_path)

    result = invoke("documents", "get", "--fiscal", "2025")

    saved = tmp_path / "INFORMACIÓN FISCAL 2025.pdf"
    assert result.exit_code == 0
    assert saved.read_bytes() == PDF
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert result.stdout == ""  # the notice goes to stderr


@pytest.mark.usefixtures("store", "lists")
def test_get_by_name_into_a_directory(mock_api: respx.MockRouter, tmp_path: Path) -> None:
    mock_api.get(f"{CPERF}/v2/customers/self/documents/signedes/{CASH_DOC_UUID}").respond(
        200, content=PDF, headers=PDF_HEADERS
    )

    invoke("documents", "get", "costes y gastos", "-o", str(tmp_path))

    assert (tmp_path / "Informe de costes y gastos 2025.pdf").read_bytes() == PDF


@pytest.mark.usefixtures("store", "lists")
def test_get_to_stdout(mock_api: respx.MockRouter) -> None:
    mock_api.get(SIGNED).respond(200, content=PDF, headers=PDF_HEADERS)

    result = invoke("documents", "get", DOC_UUID, "-o", "-")

    assert result.stdout_bytes == PDF


@pytest.mark.usefixtures("store", "lists")
def test_get_ambiguous_name_needs_latest(
    mock_api: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_api.get(SIGNED).respond(200, content=PDF, headers=PDF_HEADERS)
    target = tmp_path / "fiscal.pdf"

    code, error = run_main(monkeypatch, capsys, "documents", "get", "fiscal", "-o", str(target))
    assert code == EXIT_USAGE and "coincide con 2 documentos" in error

    invoke("documents", "get", "fiscal", "--latest", "-o", str(target))
    assert target.read_bytes() == PDF


@pytest.mark.usefixtures("store", "lists")
def test_get_never_overwrites_without_force(
    mock_api: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    route = mock_api.get(SIGNED).respond(200, content=PDF, headers=PDF_HEADERS)
    target = tmp_path / "mio.pdf"
    target.write_bytes(b"mi copia")

    code, error = run_main(monkeypatch, capsys, "documents", "get", DOC_UUID, "-o", str(target))

    assert code == EXIT_USAGE and "ya existe" in error and "--force" in error
    assert target.read_bytes() == b"mi copia"
    assert not route.called  # checked before downloading
    invoke("documents", "get", DOC_UUID, "-o", str(target), "--force")
    assert target.read_bytes() == PDF


@pytest.mark.usefixtures("store")
def test_get_sanitizes_the_name_from_the_api(
    mock_api: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_api.get(f"{CPERF}/v2/customers/self/documents/fiscal").respond(
        200, json=ok([document(DOC_UUID, "../../.ssh/authorized_keys")])
    )
    for path in ("cash-accounts", "securities-accounts", "pension-accounts", "portfolios"):
        mock_api.get(f"{CPERF}/v2/{path}/self/documents").respond(200, json=ok([]))
    mock_api.get(SIGNED).respond(200, content=PDF, headers=PDF_HEADERS)
    inside = tmp_path / "dentro"
    inside.mkdir()
    monkeypatch.chdir(inside)

    invoke("documents", "get", DOC_UUID)

    assert [p.name for p in inside.iterdir()] == ["_.._.ssh_authorized_keys.pdf"]
    assert [p.name for p in tmp_path.iterdir()] == ["dentro"]


@pytest.mark.usefixtures("store", "lists")
def test_get_rejects_html_and_leaves_no_file(
    mock_api: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_api.get(SIGNED).respond(200, text="<html>login</html>", headers={"content-type": "text/html"})

    code, error = run_main(monkeypatch, capsys, "documents", "get", DOC_UUID, "-o", str(tmp_path))

    assert code == 1 and "no es un PDF" in error
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("store", "lists")
def test_get_size_limit(
    mock_api: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(documents, "MAX_PDF_BYTES", 32)
    mock_api.get(SIGNED).respond(200, content=PDF, headers=PDF_HEADERS)

    code, error = run_main(monkeypatch, capsys, "documents", "get", DOC_UUID, "-o", str(tmp_path))

    assert code == 1 and "demasiado grande" in error
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("store", "lists")
def test_get_refreshes_on_401(mock_api: respx.MockRouter, tmp_path: Path) -> None:
    new_access = make_jwt(given_name="Nueva")
    mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens(new_access))
    route = mock_api.get(SIGNED)
    route.side_effect = [httpx.Response(401), httpx.Response(200, content=PDF, headers=PDF_HEADERS)]

    invoke("documents", "get", DOC_UUID, "-o", str(tmp_path / "f.pdf"))

    assert (tmp_path / "f.pdf").read_bytes() == PDF
    assert route.calls.last.request.headers["authorization"] == f"Bearer {new_access}"


@pytest.mark.usefixtures("store", "lists")
def test_get_needs_one_of_query_or_fiscal(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_main(monkeypatch, capsys, "documents", "get")[0] == EXIT_USAGE
    assert run_main(monkeypatch, capsys, "documents", "get", "x", "--fiscal", "2025")[0] == EXIT_USAGE


# ---- summary and xray ---------------------------------------------------------


@pytest.mark.usefixtures("store")
def test_summary_of_a_cash_account(mock_api: respx.MockRouter, tmp_path: Path) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1, alias="Mi cuenta")]))
    mock_api.get(f"{CPERF}/v2/cash-accounts/101/documents/summary").respond(
        200, json=ok({"id": "tttttttt-0000-0000-0000-000000000001", "fileName": "Extracto cuenta.pdf"})
    )
    mock_api.get(f"{CPERF}/v2/documents/temporary/tttttttt-0000-0000-0000-000000000001").respond(
        200, content=PDF, headers=PDF_HEADERS
    )

    invoke("documents", "summary", "-a", "Mi cuenta", "-o", str(tmp_path))

    assert (tmp_path / "Extracto cuenta.pdf").read_bytes() == PDF


@pytest.mark.usefixtures("store")
def test_xray_of_a_fund(mock_api: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_api.get(f"{CPERF}/v2/documents/funds/IE0000000001/x-ray").respond(200, content=PDF, headers=PDF_HEADERS)
    monkeypatch.chdir(tmp_path)

    invoke("xray", "--isin", "IE0000000001")

    assert (tmp_path / "radiografia-fondo-IE0000000001.pdf").read_bytes() == PDF


@pytest.mark.usefixtures("store")
def test_xray_needs_exactly_one_target(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_main(monkeypatch, capsys, "xray")[0] == EXIT_USAGE
    assert run_main(monkeypatch, capsys, "xray", "-a", "201", "--isin", "IE0000000001")[0] == EXIT_USAGE


@pytest.mark.usefixtures("store", "lists")
def test_long_error_stays_on_one_line_without_a_terminal(
    mock_api: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Redirected stderr: a 200-character path never splits the message (#67)."""
    mock_api.get(SIGNED).respond(200, content=PDF, headers=PDF_HEADERS)
    folder = tmp_path / ("ruta-muy-larga-" * 14)
    folder.mkdir()
    target = folder / "mio.pdf"
    target.write_bytes(b"mi copia")
    assert len(str(target)) > 200

    code, error = run_main(monkeypatch, capsys, "documents", "get", DOC_UUID, "-o", str(target))

    assert code == EXIT_USAGE
    assert error.count("\n") == 1 and error.endswith("\n")
    assert str(target) in error and "ya existe" in error


@pytest.mark.parametrize("as_json", [False, True])
def test_fail_never_wraps_a_long_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    monkeypatch.setattr(requested, "json", as_json)
    message = "/" + "carpeta/" * 40 + "mio.pdf ya existe: usa --force"

    with pytest.raises(SystemExit):
        _fail(InputError(message), "Error", message)

    error = capsys.readouterr().err
    assert error.count("\n") == 1 and message in (json.loads(error)["message"] if as_json else error)
