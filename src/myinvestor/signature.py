"""Second-factor approvals ("firmas"), as the web app runs them (`Rbe` in its bundle).

A protected call may answer 202 with a signature request instead of data:

    {"signatureRequestId": "…", "status": "PENDING", "pendingSignatureMethods": [["INAPP"]]}

`pendingSignatureMethods` is a list of groups; each group is a factor still missing and lists
alternative methods for it. For each method we *start* it (`POST …/signature/{id}?method=M`, which
sends the SMS or the push), then either validate a code (`POST …/{id}/validate/{signatureId}`)
or, for an approval in the mobile app (INAPP), poll the request every 3 s until it leaves PENDING.
Finally the original call is repeated with `x-signature-id: <signatureRequestId>`.

Before login the same endpoints live under `/signature/api/v3/public/…`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from myinvestor.errors import ApiError, SignatureError, SignatureRequired

PENDING = "PENDING"
COMPLETE = "COMPLETE"
INAPP = "INAPP"
OTP_METHODS = frozenset({"OTP_SMS", "OTP_EMAIL", "OTP_PUSH"})
SUPPORTED_METHODS = OTP_METHODS | {INAPP}

MAX_CODE_ATTEMPTS = 3  # the web app allows three tries per code (`kbe`)
WRONG_CODE_STATUSES = frozenset({400, 422})
POLL_INTERVAL = 3.0  # web app: `refetchInterval: 3e3`
DEFAULT_APPROVAL_TIMEOUT = 300.0

SIGNATURE_HEADER = "x-signature-id"


@dataclass(frozen=True)
class SignatureRequest:
    id: str
    status: str
    pending_methods: list[list[str]] = field(default_factory=list)
    description: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == PENDING

    def is_waiting_for(self, method: str) -> bool:
        return any(method in group for group in self.pending_methods)


@dataclass(frozen=True)
class Challenge:
    """A started method: `channel`/`destination` say where the code went (masked phone, email)."""

    signature_id: str
    method: str
    channel: str | None = None
    destination: str | None = None


def parse_signature_request(data: Any) -> SignatureRequest | None:
    """The 202 payload (or a status poll) if it describes a signature request, else None."""
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        return None
    request_id, status, methods = (
        data.get("signatureRequestId"),
        data.get("status"),
        data.get("pendingSignatureMethods"),
    )
    if not isinstance(request_id, str) or not isinstance(status, str):
        return None
    groups = [[str(m) for m in group] for group in methods or [] if isinstance(group, list)]
    description = data.get("operativeDescription")
    return SignatureRequest(request_id, status, groups, str(description) if description else None)


class SignatureUI(Protocol):
    """How the CLI talks to the person: ask for a code, or tell them to approve in the app."""

    def ask_code(self, challenge: Challenge, attempt: int) -> str | None: ...
    def approve_in_app(self, request: SignatureRequest) -> None: ...


Call = Callable[..., Any]  # (method, path, *, params=None, json=None) -> response payload `data`


class Signer:
    def __init__(
        self,
        call: Call,
        *,
        public: bool = False,
        timeout: float = DEFAULT_APPROVAL_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._call = call
        self._base = "/signature/api/v3/public/signature" if public else "/signature/api/v3/signature"
        self._timeout = timeout
        self._poll = poll_interval
        self._sleep = sleep or time.sleep  # resolved per instance, so tests can patch `time`
        self._clock = clock or time.monotonic

    def complete(self, request: SignatureRequest, ui: SignatureUI | None) -> SignatureRequest:
        """Run every pending factor; returns the request once COMPLETE or raises SignatureError."""
        tried: set[str] = set()
        while request.is_pending:
            if not request.pending_methods:  # last factor accepted, COMPLETE not set yet
                request = self._settle(request)
                continue
            method = _next_method(request, tried)
            if method is None:
                raise SignatureError(_no_method_message(request, tried))
            if ui is None:
                raise SignatureRequired(sorted({m for g in request.pending_methods for m in g}))
            tried.add(method)
            challenge = self._start(request.id, method)
            if method == INAPP:
                ui.approve_in_app(request)
                request = self._wait_in_app(request)
            else:
                request = self._enter_code(request, challenge, ui)
        if request.status != COMPLETE:
            raise SignatureError(f"La firma ha terminado en estado {request.status}.")
        return request

    def _start(self, request_id: str, method: str) -> Challenge:
        data = self._call("POST", f"{self._base}/{request_id}", params={"method": method})
        if not isinstance(data, dict) or not data.get("signatureId"):
            raise SignatureError(f"Respuesta inesperada al iniciar la firma {method}.")
        return Challenge(
            signature_id=str(data["signatureId"]),
            method=method,
            channel=data.get("channel"),
            destination=data.get("destination"),
        )

    def _status(self, request_id: str) -> SignatureRequest:
        data = self._call("GET", self._base, params={"signatureRequestId": request_id})
        parsed = parse_signature_request(data)
        if parsed is None:
            raise SignatureError("Respuesta inesperada al consultar el estado de la firma.")
        return parsed

    def _wait_in_app(self, request: SignatureRequest) -> SignatureRequest:
        deadline = self._clock() + self._timeout
        while True:
            self._sleep(self._poll)
            request = self._status(request.id)
            if request.status == "CANCELLED":
                raise SignatureError("Has rechazado la aprobación en la app (o MyInvestor la ha cancelado).")
            if not request.is_pending or not request.is_waiting_for(INAPP):
                return request
            if self._clock() >= deadline:
                raise SignatureError(f"No se ha aprobado en la app en {int(self._timeout)} s.")

    def _settle(self, request: SignatureRequest) -> SignatureRequest:
        deadline = self._clock() + self._timeout
        while request.is_pending and not request.pending_methods:
            if self._clock() >= deadline:
                raise SignatureError("La firma sigue pendiente sin métodos que completar.")
            self._sleep(self._poll)
            request = self._status(request.id)
        return request

    def _enter_code(self, request: SignatureRequest, challenge: Challenge, ui: SignatureUI) -> SignatureRequest:
        for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
            code = ui.ask_code(challenge, attempt)
            if not code:
                raise SignatureError("Firma cancelada.")
            path = f"{self._base}/{request.id}/validate/{challenge.signature_id}"
            try:
                data = self._call("POST", path, json={"code": code.strip()})
            except ApiError as exc:
                if exc.status not in WRONG_CODE_STATUSES:
                    raise
                continue  # a wrong code may come back as a 4xx instead of a still-pending request
            updated = parse_signature_request(data) or self._status(request.id)
            if not (updated.is_pending and updated.is_waiting_for(challenge.method)):
                return updated
            request = updated
        raise SignatureError(f"Código incorrecto {MAX_CODE_ATTEMPTS} veces.")


def _next_method(request: SignatureRequest, tried: set[str]) -> str | None:
    for group in request.pending_methods:
        for method in group:
            if method in SUPPORTED_METHODS and method not in tried:
                return method
    return None


def _no_method_message(request: SignatureRequest, tried: set[str]) -> str:
    repeated = sorted({m for g in request.pending_methods for m in g} & tried)
    if repeated:
        return f"MyInvestor sigue pidiendo {', '.join(repeated)} tras completarlo. Prueba otra vez más tarde."
    return (
        "MyInvestor pide un método de firma que el CLI no soporta "
        f"({_describe(request.pending_methods)}). Haz la operación en la web o la app."
    )


def _describe(groups: list[list[str]]) -> str:
    return " + ".join("/".join(group) for group in groups) or "?"
