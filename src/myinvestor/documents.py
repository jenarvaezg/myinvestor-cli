"""Documents: the lists of PDFs, picking one by id or name, and checking a downloaded PDF.

PDFs carry personal and tax data. The download is streamed with a size cap and must really be a PDF
(`content-type` and the `%PDF` magic bytes), so an HTML error page or a JSON envelope never ends up
saved as a document; the file name built from the API's `name` cannot leave the target directory.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Iterator, Sequence
from enum import StrEnum
from typing import Any

import httpx
from pydantic import field_validator

from myinvestor.errors import ApiError, InputError
from myinvestor.models import _day, _Model

MAX_PDF_BYTES = 50 * 1024 * 1024
PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/octet-stream"})
PDF_MAGIC = b"%PDF"
MAX_FILENAME_BYTES = 200  # whole name, ".pdf" included: well under the usual 255-byte limit
FALLBACK_NAME = "documento"
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_SPACES = re.compile(r"\s+")


class DocumentKind(StrEnum):
    """Which list a document comes from (one endpoint each)."""

    fiscal = "fiscal"
    cash = "cash"
    securities = "securities"
    pension = "pension"
    portfolio = "portfolio"


class XrayTarget(StrEnum):
    """What an «x-ray» (portfolio report generated on the fly) is about; the value is its path segment."""

    securities = "securities"
    pension = "pension-plans"
    fund = "funds"


class Document(_Model):
    """An entry of a document list. `kind` is not in the API: it is the list it came from."""

    kind: DocumentKind
    document_uuid: str
    document_id: str | None = None
    name: str = ""
    extension: str | None = None
    is_notified: bool | None = None
    create_date: dt.date | None = None
    sign_date: dt.date | None = None
    account_id: str | None = None
    account_code: str | None = None
    account_alias: str | None = None
    account_type: str | None = None

    _days = field_validator("create_date", "sign_date", mode="before")(_day)

    @classmethod
    def from_api(cls, row: dict[str, Any], kind: DocumentKind) -> Document:
        return cls.model_validate({**row, "kind": kind})

    @property
    def account_label(self) -> str | None:
        return self.account_alias or self.account_code or self.account_id or None


class TemporaryDocument(_Model):
    """`{id, fileName}`: a PDF generated on request (account statement), downloaded by `id`."""

    id: str
    file_name: str = ""


def document_rows(docs: Sequence[Document]) -> list[dict[str, Any]]:
    """Newest first; `cuenta` is the alias, else the code, else the id of the account (if any)."""
    ordered = sorted(docs, key=lambda d: (d.create_date or dt.date.min, d.kind.value, d.name), reverse=True)
    return [
        {
            "id": d.document_uuid,
            "tipo": d.kind.value,
            "nombre": d.name,
            "fecha": d.create_date.isoformat() if d.create_date else None,
            "cuenta": d.account_label,
            "notificado": d.is_notified,
        }
        for d in ordered
    ]


# ---- picking a document -----------------------------------------------------


def fold(text: str) -> str:
    """Case- and accent-insensitive form of a name, without a trailing ".pdf"."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    folded = _SPACES.sub(" ", plain).strip().casefold()
    return folded.removesuffix(".pdf").rstrip()


def find_documents(docs: Sequence[Document], query: str) -> list[Document]:
    """By `documentUuid` or `documentId`; else by name: exact (ignoring case and accents), else contained."""
    by_id = {d.document_uuid: d for d in docs if query.strip() in (d.document_uuid, d.document_id)}
    if by_id:
        return list(by_id.values())
    wanted = fold(query)
    exact = [d for d in docs if fold(d.name) == wanted]
    return exact or ([d for d in docs if wanted in fold(d.name)] if wanted else [])


def fiscal_name(year: int) -> str:
    return f"INFORMACIÓN FISCAL {year}"


def pick_document(matches: Sequence[Document], query: str, *, latest: bool = False) -> Document:
    if not matches:
        raise InputError(f"Ningún documento tiene el id o el nombre «{query}». Míralos con `myinvestor documents`.")
    if len(matches) == 1 or latest:
        return max(matches, key=lambda d: d.create_date or dt.date.min)
    listed = "; ".join(f"«{d.name}» ({d.create_date or '?'}, {d.document_uuid})" for d in matches[:10])
    raise InputError(f"«{query}» coincide con {len(matches)} documentos: {listed}. Usa su id o --latest.")


# ---- file names -------------------------------------------------------------


def safe_filename(name: str) -> str:
    """A single file name from an API-provided name: no separators, control chars, leading dots or `..`."""
    text = _UNSAFE_CHARS.sub("_", unicodedata.normalize("NFC", name))
    text = _SPACES.sub(" ", text).strip()
    stem = text[:-4] if text.lower().endswith(".pdf") else text
    stem = stem.lstrip(". ").rstrip(". ")
    stem = stem.encode()[: MAX_FILENAME_BYTES - len(".pdf")].decode(errors="ignore").rstrip(". ")
    if not stem.strip("._ "):
        stem = FALLBACK_NAME
    return f"{stem}.pdf"


# ---- checking the PDF while it streams --------------------------------------


def _not_pdf(response: httpx.Response, why: str) -> ApiError:
    return ApiError(response.status_code, f"la respuesta no es un PDF ({why})", response.request.url.path)


def _too_large(response: httpx.Response, max_bytes: int) -> ApiError:
    megabytes = max_bytes / (1024 * 1024)
    return ApiError(
        response.status_code, f"documento demasiado grande (más de {megabytes:g} MB)", response.request.url.path
    )


def pdf_chunks(response: httpx.Response, *, max_bytes: int | None = None) -> Iterator[bytes]:
    """The body of an open streamed response, checked to be a PDF of at most `max_bytes` (`MAX_PDF_BYTES`).

    Raises `ApiError` before yielding anything if the `content-type` is not PDF/octet-stream or the
    body does not start with `%PDF`, and as soon as more than `max_bytes` arrive (it stops reading).
    """
    max_bytes = MAX_PDF_BYTES if max_bytes is None else max_bytes
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in PDF_CONTENT_TYPES:
        raise _not_pdf(response, f"content-type {content_type or 'vacío'}")
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise _too_large(response, max_bytes)
    head, size, checked = b"", 0, False
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise _too_large(response, max_bytes)
        if checked:
            yield chunk
            continue
        head += chunk
        if len(head) < len(PDF_MAGIC):
            continue
        if not head.startswith(PDF_MAGIC):
            raise _not_pdf(response, "no empieza por %PDF")
        checked = True
        yield head
    if not checked:
        raise _not_pdf(response, "vacía" if not head else "no empieza por %PDF")
