"""#23: only what is new since the last run (`--new` + local state, `--since-date`, `--since-id`, `state`)."""

from __future__ import annotations

import json
import os
import stat
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from myinvestor import cli_common
from myinvestor.cli import app
from myinvestor.errors import InputError
from myinvestor.psd2 import free_start
from myinvestor.session import Session
from myinvestor.state import (
    AccountCursor,
    CorruptStateError,
    Marks,
    SeenState,
    StateFile,
    StateKind,
    default_state_path,
    merge_cursor,
    movement_marks,
    state_from_dict,
    state_to_dict,
    unseen_movements,
)
from myinvestor.stores import MemorySessionStore
from tests.conftest import CASH_UUID, CPERF, cash_account, flow, ok, order

runner = CliRunner()
SECOND_UUID = "aaaaaaaa-0000-0000-0000-000000000002"
FLOWS = r".*/account/api/v3/account/(?P<uuid>[^/]+)/flows"
TODAY = date.today()
DAY = timedelta(days=1)
CONTRIBUTIONS = f"{CPERF}/v2/automatic-contributions/self"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, session: Session) -> MemorySessionStore:
    memory = MemorySessionStore(session)
    monkeypatch.setattr(cli_common, "get_store", lambda: memory)
    return memory


def on(day: date, i: int, amount: float, **overrides: Any) -> dict[str, Any]:
    stamp = f"{day.isoformat()}T00:00:00.000+0000"
    return flow(i, amount, operationDate=stamp, valueDate=stamp, **overrides)


class Flows:
    """Flows endpoint over a mutable list of movements per account, filtering by operation date."""

    def __init__(self, by_account: dict[str, list[dict[str, Any]]]) -> None:
        self.by_account = by_account
        self.requests: list[tuple[str, str, str]] = []

    def __call__(self, request: httpx.Request, uuid: str) -> httpx.Response:
        start, end = request.url.params["dateFrom"], request.url.params["dateTo"]
        self.requests.append((uuid, start, end))
        rows = [f for f in self.by_account.get(uuid, []) if start <= f["operationDate"][:10] <= end]
        return httpx.Response(200, json=ok({"flowList": rows}))


@pytest.fixture
def flows(mock_api: respx.MockRouter, store: MemorySessionStore) -> Flows:
    fake = Flows({CASH_UUID: [on(TODAY - 2 * DAY, 1, -10.0), on(TODAY - DAY, 2, 20.0)], SECOND_UUID: []})
    mock_api.get(f"{CPERF}/v2/cash-accounts/self").respond(200, json=ok([cash_account(1), cash_account(2)]))
    mock_api.get(url__regex=FLOWS).mock(side_effect=fake)
    return fake


def run(*args: str) -> Any:
    return runner.invoke(app, list(args), catch_exceptions=False)


def ids(result: Any) -> list[str]:
    assert result.exit_code == 0, result.output
    return [r["id"] for r in json.loads(result.stdout)]


# ---- pure model -----------------------------------------------------------------------------


def test_merge_cursor_keeps_the_later_day_and_prunes_ids_older_than_the_margin() -> None:
    old = AccountCursor(date(2026, 10, 1), {"a": date(2026, 9, 20), "b": date(2026, 9, 29)})
    new = AccountCursor(date(2026, 10, 10), {"c": date(2026, 10, 9)})

    merged = merge_cursor(old, new)

    assert merged.last_day == date(2026, 10, 10)
    assert merged.next_start() == date(2026, 10, 7)
    assert set(merged.seen) == {"c"}  # a and b are older than the next start: never asked again
    assert merge_cursor(new, old).last_day == date(2026, 10, 10)  # never goes back


def test_unseen_and_marks_are_per_account() -> None:
    rows = [
        {"cuenta_uuid": "u1", "id": "u1:1", "fecha": "2026-10-09"},
        {"cuenta_uuid": "u1", "id": "u1:2", "fecha": "2026-10-10"},
        {"cuenta_uuid": "u2", "id": "u2:1", "fecha": "2026-10-10"},
    ]
    cursors = {"u1": AccountCursor(date(2026, 10, 9), {"u1:1": date(2026, 10, 9)})}

    assert [r["id"] for r in unseen_movements(rows, cursors)] == ["u1:2", "u2:1"]
    marks = movement_marks(rows[:2], ["u1", "u3"], date(2026, 10, 10))
    assert set(marks.movements) == {"u1", "u3"}
    assert marks.movements["u3"] == AccountCursor(date(2026, 10, 10), {})


def test_state_round_trips_and_pending_is_separate() -> None:
    marks = Marks(
        movements={"u1": AccountCursor(date(2026, 10, 10), {"u1:1": date(2026, 10, 9)})},
        snapshots={StateKind.orders: {"r1": "EXECUTED"}},
    )
    state = SeenState().mark(marks).defer(Marks(snapshots={StateKind.contributions: {"c1": {"estado": "A"}}}))

    assert state_from_dict(json.loads(json.dumps(state_to_dict(state)))) == state
    committed = state.commit(StateKind.contributions)
    assert committed.pending.is_empty()
    assert committed.marked.snapshot(StateKind.contributions) == {"c1": {"estado": "A"}}
    assert state.reset(StateKind.orders).marked.snapshot(StateKind.orders) == {}
    assert state.reset().marked.is_empty()


def test_corrupt_state_file_is_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    path.write_text("{no json")
    with pytest.raises(CorruptStateError, match="state reset"):
        StateFile(path).load()


# ---- movements --new ---------------------------------------------------------------------------


def test_two_runs_of_new_do_not_repeat(flows: Flows) -> None:
    assert ids(run("movements", "--new", "-f", "json")) == [f"{CASH_UUID}:ref2", f"{CASH_UUID}:ref1"]

    assert ids(run("movements", "--new", "-f", "json")) == []
    since = (TODAY - 3 * DAY).isoformat()  # the last day asked for, minus the 3-day margin
    assert {(u, s) for u, s, _ in flows.requests[-2:]} == {(CASH_UUID, since), (SECOND_UUID, since)}


def test_new_emits_only_what_appeared_since(flows: Flows) -> None:
    run("movements", "--new", "-f", "json")
    flows.by_account[CASH_UUID].append(on(TODAY - 3 * DAY, 3, 5.0))  # booked late, with an older date
    flows.by_account[SECOND_UUID].append(on(TODAY, 4, 7.0))

    assert ids(run("movements", "--new", "-f", "json")) == [f"{SECOND_UUID}:ref4", f"{CASH_UUID}:ref3"]


def test_state_file_is_private_and_in_xdg_state_home(flows: Flows) -> None:
    run("movements", "--new", "-f", "json")

    path = default_state_path()
    assert path == Path(os.environ["XDG_STATE_HOME"]) / "myinvestor" / "seen.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    data = json.loads(path.read_text())
    assert data["movements"][CASH_UUID]["last_day"] == TODAY.isoformat()
    assert data["movements"][SECOND_UUID] == {"last_day": TODAY.isoformat(), "seen": {}}


def test_no_mark_does_not_mark_until_state_mark(flows: Flows, tmp_path: Path) -> None:
    state = str(tmp_path / "custom" / "seen.json")
    first = ids(run("movements", "--new", "--no-mark", "--state", state, "-f", "json"))

    result = run("movements", "--new", "--no-mark", "--state", state, "-f", "json")
    assert ids(result) == first  # nothing was marked
    assert "state mark movements" in result.stderr

    assert run("state", "mark", "--state", state).exit_code == 0
    assert ids(run("movements", "--new", "--state", state, "-f", "json")) == []


def test_old_state_is_clipped_to_the_free_window_with_a_warning(flows: Flows) -> None:
    old = TODAY - 200 * DAY
    StateFile(default_state_path()).update(
        lambda s: s.mark(Marks(movements={CASH_UUID: AccountCursor(old, {f"{CASH_UUID}:x": old})}))
    )

    result = run("movements", "--new", "-a", "Cuenta 1", "-f", "json")

    assert ids(result) == [f"{CASH_UUID}:ref2", f"{CASH_UUID}:ref1"]
    assert flows.requests == [(CASH_UUID, free_start(TODAY).isoformat(), TODAY.isoformat())]
    assert "antes de la ventana sin firma" in " ".join(result.stderr.split())
    seen = StateFile(default_state_path()).load().marked.movements[CASH_UUID].seen
    assert f"{CASH_UUID}:x" not in seen  # pruned: older than the next start


def test_first_run_of_new_never_goes_beyond_the_free_window(flows: Flows) -> None:
    result = run("movements", "--new", "--days", "300", "-a", "Cuenta 1", "-f", "json")

    assert result.exit_code == 0
    assert flows.requests == [(CASH_UUID, free_start(TODAY).isoformat(), TODAY.isoformat())]


@pytest.mark.parametrize("extra", [["--to", "2026-10-01"], ["--since-date", "2026-10-01"], ["--search", "x"]])
def test_new_rejects_options_that_break_the_cursor(store: MemorySessionStore, extra: list[str]) -> None:
    result = runner.invoke(app, ["movements", "--new", *extra])
    assert isinstance(result.exception, InputError)


# ---- --since-date / --since-id ---------------------------------------------------------------------


def test_since_date_is_strictly_after_the_day(flows: Flows) -> None:
    day = (TODAY - 2 * DAY).isoformat()

    assert ids(run("movements", "--since-date", day, "-a", "Cuenta 1", "-f", "json")) == [f"{CASH_UUID}:ref2"]
    assert flows.requests == [(CASH_UUID, (TODAY - DAY).isoformat(), TODAY.isoformat())]


def test_since_date_today_gives_nothing_without_asking(flows: Flows) -> None:
    assert ids(run("movements", "--since-date", TODAY.isoformat(), "-f", "json")) == []
    assert flows.requests == []


def test_since_id_gives_what_comes_after_it_in_its_account(flows: Flows) -> None:
    flows.by_account[CASH_UUID] += [on(TODAY - DAY, 3, 1.0), on(TODAY - 5 * DAY, 4, 1.0)]
    flows.by_account[SECOND_UUID] = [on(TODAY, 5, 1.0)]

    result = run("movements", "--since-id", f"{CASH_UUID}:ref2", "--oldest-first", "-f", "json")

    assert ids(result) == [f"{CASH_UUID}:ref3"]  # same day, later ref; the other account is not asked
    assert {u for u, _, _ in flows.requests} == {CASH_UUID}


def test_since_id_unknown_is_an_input_error(flows: Flows) -> None:
    result = runner.invoke(app, ["movements", "--since-id", f"{CASH_UUID}:nope"])
    assert isinstance(result.exception, InputError)
    assert "--since-id" in str(result.exception)


def test_since_date_and_from_clash(store: MemorySessionStore) -> None:
    result = runner.invoke(app, ["movements", "--since-date", "2026-10-01", "--days", "3"])
    assert isinstance(result.exception, InputError)


# ---- orders / contributions --new -------------------------------------------------------------------


def test_orders_new_reports_new_orders_and_status_changes(
    mock_api: respx.MockRouter, store: MemorySessionStore
) -> None:
    mock_api.get(f"{CPERF}/v2/securities-accounts/self-basic").respond(200, json=ok([{"accountId": "201"}]))
    route = mock_api.get(f"{CPERF}/v3/securities-accounts/201/orders")
    route.respond(200, json=ok([order("a", "2026-10-01"), order("b", "2026-10-02", status="IN_PROGRESS")]))

    assert [r["ref"] for r in json.loads(run("orders", "--new", "-f", "json").stdout)] == ["b", "a"]
    assert json.loads(run("orders", "--new", "-f", "json").stdout) == []

    route.respond(200, json=ok([order("a", "2026-10-01"), order("b", "2026-10-02"), order("c", "2026-10-03")]))
    rows = json.loads(run("orders", "--new", "-f", "json").stdout)
    assert [(r["ref"], r["estado"]) for r in rows] == [("c", "EXECUTED"), ("b", "EXECUTED")]
    saved = StateFile(default_state_path()).load().marked.snapshot(StateKind.orders)
    assert saved == {"a": "EXECUTED", "b": "EXECUTED", "c": "EXECUTED"}


def contribution(cid: str, nxt: str, status: str = "ACTIVE") -> dict[str, Any]:
    return {
        "contributionId": cid,
        "amount": 100.0,
        "status": status,
        "isin": "IE0000000001",
        "contributionTimeFrame": {"recurrence": "MONTHLY", "nextExecutionDate": f"{nxt}T00:00:00.000Z"},
    }


def test_contributions_new_reports_changes_of_next_date(mock_api: respx.MockRouter, store: MemorySessionStore) -> None:
    route = mock_api.get(CONTRIBUTIONS)
    route.respond(200, json=ok([contribution("c1", "2026-11-05"), contribution("c2", "2026-11-20")]))
    assert len(json.loads(run("contributions", "--new", "--no-mark", "-f", "json").stdout)) == 2
    assert len(json.loads(run("contributions", "--new", "-f", "json").stdout)) == 2  # --no-mark kept them new
    assert json.loads(run("contributions", "--new", "-f", "json").stdout) == []

    route.respond(200, json=ok([contribution("c1", "2026-12-05"), contribution("c2", "2026-11-20")]))
    assert [r["id"] for r in json.loads(run("contributions", "--new", "-f", "json").stdout)] == ["c1"]


# ---- state show / reset ------------------------------------------------------------------------------


def test_state_show_and_reset(flows: Flows) -> None:
    run("movements", "--new", "-f", "json")

    shown = json.loads(run("state", "show", "-f", "json").stdout)
    assert shown["path"] == str(default_state_path())
    assert set(shown["movements"][CASH_UUID]["seen"]) == {f"{CASH_UUID}:ref1", f"{CASH_UUID}:ref2"}
    table = run("state", "show", "-f", "csv").stdout
    assert f"movements,{CASH_UUID},{TODAY.isoformat()},2,0" in table

    assert run("state", "reset", "movements").exit_code == 0
    assert len(ids(run("movements", "--new", "-f", "json"))) == 2
    assert run("state", "reset").exit_code == 0
    assert StateFile(default_state_path()).load() == SeenState()


def test_state_reset_works_on_a_corrupt_file() -> None:
    path = default_state_path()
    path.parent.mkdir(parents=True)
    path.write_text("garbage")

    assert run("state", "reset").exit_code == 0
    assert StateFile(path).load() == SeenState()


def test_new_rows_carry_the_internal_transfer_fields(flows: Flows) -> None:
    rows = json.loads(run("movements", "--new", "-f", "json").stdout)

    assert rows and all(r["pareja_id"] is None and r["traspaso_interno"] is False for r in rows)
