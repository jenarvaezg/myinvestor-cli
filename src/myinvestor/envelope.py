"""Decode MyInvestor responses: `{"payload": {"data": …, "pagination": …}, "status": {…}}`.

Amounts come as JSON floats in euros; they are parsed straight into `Decimal` so no binary
rounding ever reaches the user. The activity feed answers `text/event-stream` with a single
`data:{…}` event, which is decoded the same way.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx

from myinvestor.errors import ApiError


def decode(response: httpx.Response) -> Any:
    text = response.text
    if not text.strip():
        return None
    if "text/event-stream" in response.headers.get("content-type", ""):
        events = [line[len("data:") :] for line in text.splitlines() if line.startswith("data:")]
        if not events:
            raise ApiError(response.status_code, "evento SSE vacío", response.request.url.path)
        text = events[-1]
    try:
        return json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ApiError(response.status_code, "respuesta no es JSON", response.request.url.path) from exc


def payload(body: Any) -> dict[str, Any]:
    if isinstance(body, dict) and isinstance(body.get("payload"), dict):
        return body["payload"]
    return {}


def data(body: Any) -> Any:
    return payload(body).get("data")
