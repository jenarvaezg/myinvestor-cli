"""Documents: lists, PDF streaming (binary path of the client), safe file names and writes (synthetic data)."""

from __future__ import annotations

import stat
from collections.abc import Iterator
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from myinvestor.atomicfile import write_new_atomic
from myinvestor.auth import REFRESH_PATH
from myinvestor.client import MyInvestorClient
from myinvestor.documents import (
    Document,
    DocumentKind,
    XrayTarget,
    find_documents,
    pdf_chunks,
    pick_document,
    safe_filename,
)
from myinvestor.errors import ApiError, InputError, MyInvestorError, NetworkBlockedError, SignatureError
from myinvestor.session import Session
from myinvestor.signature import Signer
from tests.conftest import API, CPERF, ok, signature_pending, tokens
from tests.test_signature import FakeUI

DOC_UUID = "dddddddd-0000-0000-0000-000000000001"
SIGNED = f"{CPERF}/v2/customers/self/documents/signedes/{DOC_UUID}"
PDF = b"%PDF-1.4\n%synthetic\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PDF_HEADERS = {"content-type": "application/pdf"}


def document(uuid: str = DOC_UUID, name: str = "INFORMACIÓN FISCAL 2025", **overrides: Any) -> dict[str, Any]:
    base = {
        "documentId": f"id-{uuid[-4:]}",
        "documentUuid": uuid,
        "name": name,
        "extension": None,
        "isNotified": False,
        "createDate": "2026-03-01T00:00:00.000+0000",
        "signDate": "2026-03-01T00:00:00.000+0000",
        "accountId": "",
        "accountCode": None,
        "accountAlias": None,
        "accountType": None,
        "isCurrentCustomerAnOwner": True,
    }
    return {**base, **overrides}


def doc(kind: DocumentKind = DocumentKind.fiscal, **fields: Any) -> Document:
    return Document.from_api(document(**fields), kind)


def client_for(session: Session, http: httpx.Client, **kwargs: Any) -> MyInvestorClient:
    return MyInvestorClient(session, http=http, **kwargs)


def download(client: MyInvestorClient, uuid: str = DOC_UUID, max_bytes: int = 1024) -> bytes:
    with client.document_pdf(uuid) as response:
        return b"".join(pdf_chunks(response, max_bytes=max_bytes))


# ---- model and lists --------------------------------------------------------


def test_document_model_and_account_label() -> None:
    d = Document.from_api(
        document(accountId="301", accountCode="P301", accountAlias=None, accountType="PENSION_PLAN"),
        DocumentKind.pension,
    )
    assert d.kind is DocumentKind.pension
    assert d.create_date == date(2026, 3, 1)
    assert d.account_label == "P301"
    assert doc().account_label is None  # accountId "" is no account
    assert Document.from_api(document(accountId="301", accountAlias="Mi plan"), DocumentKind.pension).account_label == (
        "Mi plan"
    )


def test_documents_joins_the_five_lists(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(f"{CPERF}/v2/customers/self/documents/fiscal").respond(200, json=ok([document()]))
    mock_api.get(f"{CPERF}/v2/cash-accounts/self/documents").respond(
        200, json=ok([document("dddddddd-0000-0000-0000-000000000002", "Costes y gastos 2025")])
    )
    mock_api.get(f"{CPERF}/v2/securities-accounts/self/documents").respond(200, json=ok([]))
    mock_api.get(f"{CPERF}/v2/pension-accounts/self/documents").respond(
        200, json=ok([document("dddddddd-0000-0000-0000-000000000003", "BOLETIN_DE_ADHESION_N0000.pdf")])
    )
    mock_api.get(f"{CPERF}/v2/portfolios/self/documents").respond(200, json=ok([]))

    docs = client_for(session, http).documents()

    assert [(d.kind, d.name) for d in docs] == [
        (DocumentKind.fiscal, "INFORMACIÓN FISCAL 2025"),
        (DocumentKind.cash, "Costes y gastos 2025"),
        (DocumentKind.pension, "BOLETIN_DE_ADHESION_N0000.pdf"),
    ]


def test_documents_of_one_kind_asks_only_that_list(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    fiscal = mock_api.get(f"{CPERF}/v2/customers/self/documents/fiscal").respond(200, json=ok([document()]))
    cash = mock_api.get(f"{CPERF}/v2/cash-accounts/self/documents").respond(200, json=ok([]))

    assert len(client_for(session, http).documents([DocumentKind.fiscal])) == 1
    assert fiscal.called and not cash.called


# ---- picking a document -----------------------------------------------------


def test_find_by_uuid_document_id_exact_name_and_substring() -> None:
    a = doc(uuid=DOC_UUID, name="INFORMACIÓN FISCAL 2025")
    b = doc(uuid="dddddddd-0000-0000-0000-000000000002", name="INFORMACIÓN FISCAL 2024")
    c = doc(DocumentKind.cash, uuid="dddddddd-0000-0000-0000-000000000003", name="Costes y gastos 2025.pdf")
    docs = [a, b, c]

    assert find_documents(docs, DOC_UUID) == [a]
    assert find_documents(docs, b.document_id or "") == [b]
    assert find_documents(docs, "informacion fiscal 2025") == [a]  # accents and case do not matter
    assert find_documents(docs, "Costes y gastos 2025") == [c]  # ".pdf" is optional
    assert find_documents(docs, "fiscal") == [a, b]
    assert find_documents(docs, "nada") == []


def test_pick_document_ambiguous_latest_and_missing() -> None:
    old = doc(uuid="dddddddd-0000-0000-0000-000000000002", name="Informe 2024", createDate="2025-03-01")
    new = doc(uuid="dddddddd-0000-0000-0000-000000000003", name="Informe 2025", createDate="2026-03-01")

    assert pick_document([new], "informe") == new
    assert pick_document([old, new], "informe", latest=True) == new
    with pytest.raises(InputError, match="coincide con 2 documentos"):
        pick_document([old, new], "informe")
    with pytest.raises(InputError, match="Ningún documento"):
        pick_document([], "informe")


# ---- file names -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("INFORMACIÓN FISCAL 2025", "INFORMACIÓN FISCAL 2025.pdf"),
        ("Informe de costes y gastos MIFID 2025.pdf", "Informe de costes y gastos MIFID 2025.pdf"),
        ("BOLETIN.PDF", "BOLETIN.pdf"),
        ("../../etc/passwd", "_.._etc_passwd.pdf"),
        ("a/b\\c:d", "a_b_c_d.pdf"),
        ("..", "documento.pdf"),
        ("", "documento.pdf"),
        (".oculto", "oculto.pdf"),
        ("línea\nnueva\x00", "línea_nueva_.pdf"),
    ],
)
def test_safe_filename(name: str, expected: str) -> None:
    assert safe_filename(name) == expected


def test_safe_filename_caps_the_length_in_bytes() -> None:
    name = safe_filename("ñ" * 500)
    assert name.endswith(".pdf") and len(name.encode()) <= 200


# ---- binary download --------------------------------------------------------


def test_downloads_a_pdf_without_decoding_it(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(SIGNED).respond(200, content=PDF, headers=PDF_HEADERS)

    assert download(client_for(session, http)) == PDF
    headers = route.calls.last.request.headers
    assert headers["authorization"] == f"Bearer {session.access_token}"
    assert headers["x-device-id"] == session.device_id


def test_octet_stream_with_charset_is_accepted(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mock_api.get(SIGNED).respond(200, content=PDF, headers={"content-type": "application/octet-stream; charset=x"})
    assert download(client_for(session, http)) == PDF


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"<html><body>Sesion caducada</body></html>", "text/html"),
        (b'{"payload": {"data": null}}', "application/json"),
        (b"<html>no es un pdf</html>", "application/pdf"),  # right header, wrong bytes
        (b"", "application/pdf"),
    ],
)
def test_rejects_what_is_not_a_pdf(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session, content: bytes, content_type: str
) -> None:
    mock_api.get(SIGNED).respond(200, content=content, headers={"content-type": content_type})
    with pytest.raises(ApiError, match="no es un PDF"):
        download(client_for(session, http))


def test_size_limit_by_content_length(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(SIGNED).respond(200, content=PDF + b"0" * 2000, headers=PDF_HEADERS)
    with pytest.raises(ApiError, match="demasiado grande"):
        download(client_for(session, http), max_bytes=1024)


def test_size_limit_while_streaming_without_content_length() -> None:
    sent: list[int] = []

    def chunks() -> Iterator[bytes]:
        yield PDF
        for _ in range(100):
            sent.append(1)
            yield b"0" * 512

    response = httpx.Response(200, content=chunks(), headers=PDF_HEADERS, request=httpx.Request("GET", SIGNED))
    with pytest.raises(ApiError, match="demasiado grande"):
        b"".join(pdf_chunks(response, max_bytes=1024))
    assert len(sent) < 100  # stopped reading at the limit


def test_401_refreshes_and_retries_the_download(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    refresh = mock_api.post(f"{API}{REFRESH_PATH}").respond(200, json=tokens())
    route = mock_api.get(SIGNED)
    route.side_effect = [httpx.Response(401), httpx.Response(200, content=PDF, headers=PDF_HEADERS)]

    assert download(client_for(session, http)) == PDF
    assert route.call_count == 2 and refresh.called


def test_waf_block_on_a_download_is_a_network_error(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mock_api.get(SIGNED).respond(403, text="<html>_Incapsula_Resource</html>", headers={"content-type": "text/html"})
    with pytest.raises(NetworkBlockedError):
        download(client_for(session, http))


def test_api_error_on_a_download(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(SIGNED).respond(404, json={"status": {"code": "NOT_FOUND", "message": "no existe"}})
    with pytest.raises(ApiError, match="no existe"):
        download(client_for(session, http))


def test_download_behind_a_second_factor(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    route = mock_api.get(SIGNED)
    route.side_effect = [
        httpx.Response(202, json=signature_pending("r1")),
        httpx.Response(200, content=PDF, headers=PDF_HEADERS),
    ]
    mock_api.post(f"{API}/signature/api/v3/signature/r1").respond(200, json=ok({"signatureId": "s1", "type": "INAPP"}))
    mock_api.get(f"{API}/signature/api/v3/signature").respond(
        200, json=ok({"signatureRequestId": "r1", "status": "COMPLETE", "pendingSignatureMethods": []})
    )
    client = client_for(session, http, signature_ui=FakeUI(), signer_factory=partial(Signer, sleep=lambda _s: None))

    assert download(client) == PDF
    assert route.calls.last.request.headers["x-signature-id"] == "r1"


def test_still_202_after_signing_fails(mock_api: respx.MockRouter, http: httpx.Client, session: Session) -> None:
    mock_api.get(SIGNED).respond(202, json=signature_pending("r1"))
    mock_api.post(f"{API}/signature/api/v3/signature/r1").respond(200, json=ok({"signatureId": "s1", "type": "INAPP"}))
    mock_api.get(f"{API}/signature/api/v3/signature").respond(
        200, json=ok({"signatureRequestId": "r1", "status": "COMPLETE", "pendingSignatureMethods": []})
    )
    client = client_for(session, http, signature_ui=FakeUI(), signer_factory=partial(Signer, sleep=lambda _s: None))
    with pytest.raises(SignatureError, match="sigue pidiendo firma"):
        download(client)


def test_binary_path_is_get_only(http: httpx.Client, session: Session) -> None:
    client = client_for(session, http)
    with pytest.raises(MyInvestorError, match="solo lectura"):
        client._open_binary("POST", "/x")


@pytest.mark.parametrize(
    ("target", "key", "path"),
    [
        (XrayTarget.securities, "201", "/v2/documents/securities/201/x-ray"),
        (XrayTarget.pension, "301", "/v2/documents/pension-plans/301/x-ray"),
        (XrayTarget.fund, "IE0000000001", "/v2/documents/funds/IE0000000001/x-ray"),
    ],
)
def test_xray_paths(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session, target: XrayTarget, key: str, path: str
) -> None:
    mock_api.get(f"{CPERF}{path}").respond(200, content=PDF, headers=PDF_HEADERS)
    with client_for(session, http).xray_pdf(target, key) as response:
        assert b"".join(pdf_chunks(response, max_bytes=1024)) == PDF


def test_account_summary_then_temporary_document(
    mock_api: respx.MockRouter, http: httpx.Client, session: Session
) -> None:
    mock_api.get(f"{CPERF}/v2/cash-accounts/101/documents/summary").respond(
        200, json=ok({"id": "tttttttt-0000-0000-0000-000000000001", "fileName": "Extracto.pdf"})
    )
    mock_api.get(f"{CPERF}/v2/documents/temporary/tttttttt-0000-0000-0000-000000000001").respond(
        200, content=PDF, headers=PDF_HEADERS
    )
    client = client_for(session, http)

    summary = client.account_summary("101")

    assert summary.file_name == "Extracto.pdf"
    with client.temporary_document_pdf(summary.id) as response:
        assert b"".join(pdf_chunks(response, max_bytes=1024)) == PDF


# ---- atomic binary write ----------------------------------------------------


def test_write_new_atomic_is_private_and_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "doc.pdf"

    assert write_new_atomic(target, [b"%PDF", b"-1.4"]) == 8

    assert target.read_bytes() == b"%PDF-1.4"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_new_atomic(target, [b"otro"])
    assert target.read_bytes() == b"%PDF-1.4"
    write_new_atomic(target, [b"nuevo"], overwrite=True)
    assert target.read_bytes() == b"nuevo"
    assert [p.name for p in tmp_path.iterdir()] == ["doc.pdf"]  # no temporary file left behind


def test_write_new_atomic_leaves_nothing_on_error(tmp_path: Path) -> None:
    def failing() -> Iterator[bytes]:
        yield b"%PDF"
        raise ApiError(200, "cortado", "/x")

    with pytest.raises(ApiError):
        write_new_atomic(tmp_path / "doc.pdf", failing())
    assert list(tmp_path.iterdir()) == []
