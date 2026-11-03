"""`documents` (list), `documents get` / `documents summary` (download a PDF) and `xray`."""

from __future__ import annotations

import re
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.markup import escape

from myinvestor.atomicfile import write_new_atomic
from myinvestor.cli_accounts import match_cash_accounts
from myinvestor.cli_common import FormatOpt, app, build_client, err
from myinvestor.documents import (
    DocumentKind,
    XrayTarget,
    document_rows,
    find_documents,
    fiscal_name,
    pdf_chunks,
    pick_document,
    safe_filename,
)
from myinvestor.errors import InputError
from myinvestor.output import OutputFormat, render

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
STDOUT = "-"
XRAY_NAMES = {XrayTarget.securities: "cuenta", XrayTarget.pension: "plan", XrayTarget.fund: "fondo"}


class KindFilter(StrEnum):
    fiscal = "fiscal"
    cash = "cash"
    securities = "securities"
    pension = "pension"
    portfolio = "portfolio"
    all = "all"


OutputOpt = Annotated[
    str | None,
    typer.Option(
        "--output",
        "-o",
        help="Fichero o directorio de destino (por defecto, el nombre del documento en el directorio actual); "
        "`-` para escribir el PDF en stdout.",
    ),
]
ForceOpt = Annotated[bool, typer.Option("--force", help="Sobrescribir el fichero si ya existe.")]

documents_app = typer.Typer(
    help="Documentos en PDF: información fiscal, costes y gastos, boletines… (los PDF tienen datos personales).",
)
app.add_typer(documents_app, name="documents")


@documents_app.callback(invoke_without_command=True)
def documents(
    ctx: typer.Context,
    kind: Annotated[KindFilter, typer.Option("--kind", "-k", help="Qué lista; por defecto, todas.")] = KindFilter.all,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Tus documentos: id, tipo, nombre, fecha, cuenta y si se notificó. Descárgalos con `documents get`."""
    if ctx.invoked_subcommand:
        return
    kinds = list(DocumentKind) if kind is KindFilter.all else [DocumentKind(kind.value)]
    with build_client() as client:
        docs = client.documents(kinds)
    render(document_rows(docs), fmt, title="Documentos")


@documents_app.command("get")
def get(
    query: Annotated[str | None, typer.Argument(help="Id (documentUuid) o nombre del documento.")] = None,
    fiscal: Annotated[
        int | None,
        typer.Option("--fiscal", min=2000, max=2100, metavar="AÑO", help="La «INFORMACIÓN FISCAL» de ese año."),
    ] = None,
    output: OutputOpt = None,
    latest: Annotated[
        bool, typer.Option("--latest", help="Si el nombre coincide con varios, el más reciente.")
    ] = False,
    force: ForceOpt = False,
) -> None:
    """Descarga un documento en PDF (permisos 0600; nunca sobrescribe sin --force).

    El nombre se busca sin distinguir mayúsculas ni tildes: primero exacto, luego contenido.
    """
    if (query is None) == (fiscal is None):
        raise InputError("Indica un id o nombre de documento, o --fiscal AÑO (uno de los dos).")
    with build_client() as client:
        if fiscal is not None:
            wanted = fiscal_name(fiscal)
            matches = find_documents(client.documents([DocumentKind.fiscal]), wanted)
        else:
            wanted = str(query)
            matches = find_documents(client.documents(), wanted)
        if not matches and UUID.fullmatch(wanted):  # not listed (yet): try the id as is
            uuid, name = wanted, wanted
        else:
            chosen = pick_document(matches, wanted, latest=latest)
            uuid, name = chosen.document_uuid, chosen.name
        dest = destination(output, safe_filename(name), force=force)
        with client.document_pdf(uuid) as response:
            save_pdf(response, dest, force=force)


@documents_app.command("summary")
def summary(
    account: Annotated[str, typer.Option("--account", "-a", help="Cuenta de efectivo: id, uuid, alias o IBAN.")],
    output: OutputOpt = None,
    force: ForceOpt = False,
) -> None:
    """Extracto de una cuenta de efectivo en PDF."""
    with build_client() as client:
        matches = match_cash_accounts(client.cash_accounts(), [account])
        if len(matches) != 1:
            raise InputError(f"--account {account!r} coincide con {len(matches)} cuentas: indica una sola.")
        account_id = matches[0].account_id
        requested = client.account_summary(account_id)
        dest = destination(output, safe_filename(requested.file_name or f"extracto-{account_id}"), force=force)
        with client.temporary_document_pdf(requested.id) as response:
            save_pdf(response, dest, force=force)


@app.command("xray")
def xray(
    account: Annotated[str | None, typer.Option("--account", "-a", help="Id de la cuenta de valores.")] = None,
    isin: Annotated[str | None, typer.Option("--isin", help="ISIN de un fondo.")] = None,
    pension: Annotated[str | None, typer.Option("--pension", help="Id de la cuenta de planes de pensiones.")] = None,
    output: OutputOpt = None,
    force: ForceOpt = False,
) -> None:
    """Radiografía (informe en PDF generado al momento) de una cuenta de valores, un fondo o un plan.

    --pension aún no se ha probado contra la API.
    """
    chosen = [
        (target, key)
        for target, key in ((XrayTarget.securities, account), (XrayTarget.fund, isin), (XrayTarget.pension, pension))
        if key
    ]
    if len(chosen) != 1:
        raise InputError("Indica una sola de --account, --isin o --pension.")
    target, key = chosen[0]
    dest = destination(output, safe_filename(f"radiografia-{XRAY_NAMES[target]}-{key}"), force=force)
    with build_client() as client, client.xray_pdf(target, key) as response:
        save_pdf(response, dest, force=force)


def destination(output: str | None, default_name: str, *, force: bool) -> Path | None:
    """Where to write (None: stdout), checked before downloading anything."""
    if output == STDOUT:
        return None
    path = Path(output).expanduser() if output else Path(default_name)
    if path.is_dir():
        path = path / default_name
    if not path.parent.is_dir():
        raise InputError(f"No existe el directorio {path.parent}.")
    if path.is_dir():
        raise InputError(f"{path} es un directorio.")
    if path.exists() and not force:
        raise InputError(f"{path} ya existe: usa --force para sobrescribirlo u -o para elegir otro nombre.")
    return path


def save_pdf(response: httpx.Response, dest: Path | None, *, force: bool) -> None:
    """Stream the checked PDF to `dest` (atomically, 0600) or, whole and only once checked, to stdout."""
    chunks = pdf_chunks(response)
    if dest is None:
        data = b"".join(chunks)
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    try:
        size = write_new_atomic(dest, chunks, overwrite=force)
    except FileExistsError as exc:
        raise InputError(f"{dest} ya existe: usa --force para sobrescribirlo u -o para elegir otro nombre.") from exc
    err.print(f"Guardado [bold]{escape(str(dest))}[/bold] ({max(size // 1024, 1)} KB, permisos 0600).")
