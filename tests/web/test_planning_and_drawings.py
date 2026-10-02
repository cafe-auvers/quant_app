from __future__ import annotations

import uuid


def command(client, operation, revision, *, command_id=None, price=None):
    return client.post(
        "/api/v1/planning/AAPL/commands",
        json={
            "command_id": command_id or str(uuid.uuid4()),
            "operation": operation,
            "expected_revision": revision,
            "breakout_price": price,
        },
    )


def test_sandbox_watchlist_breakout_buylist_flow_persists(authenticated):
    added = command(authenticated, "add_watchlist", 0)
    assert added.status_code == 200
    assert added.json()["card"]["stage"] == "WATCHLIST"
    assert added.json()["card"]["sync_state"] == "NOT SYNCED TO EXECUTOR"

    breakout = command(authenticated, "set_breakout", 1, price=192.75)
    assert breakout.status_code == 200
    assert breakout.json()["card"]["breakout_price"] == 192.75

    promoted = command(authenticated, "promote_buylist", 2)
    assert promoted.status_code == 200
    assert promoted.json()["card"]["stage"] == "BUYLIST"
    assert authenticated.get("/api/v1/planning/AAPL").json()["card"]["version"] == 3

    preview = authenticated.post(
        "/api/v1/planning/AAPL/buy-today-preview",
        json={"expected_revision": 3},
    )
    assert preview.status_code == 200
    assert preview.json()["executable"] is False
    assert preview.json()["published"] is False

    drafts = authenticated.get("/api/v1/buy-today-drafts")
    assert drafts.status_code == 200
    assert drafts.json()["executable"] is False
    assert drafts.json()["rows"][0]["symbol"] == "AAPL"
    assert drafts.json()["rows"][0]["stage"] == "BUY_TODAY_DRAFT"

    cancelled = authenticated.request(
        "DELETE",
        "/api/v1/planning/AAPL/buy-today-preview",
        json={"expected_revision": 3},
    )
    assert cancelled.status_code == 200
    assert cancelled.json() == {
        "cancelled": True,
        "published": False,
        "executable": False,
    }
    assert authenticated.get("/api/v1/buy-today-drafts").json()["rows"] == []
    assert authenticated.get("/api/v1/planning/AAPL").json()["card"]["stage"] == "BUYLIST"

    cancelled_again = authenticated.request(
        "DELETE",
        "/api/v1/planning/AAPL/buy-today-preview",
        json={"expected_revision": 3},
    )
    assert cancelled_again.status_code == 200
    assert cancelled_again.json()["cancelled"] is False

    activation = authenticated.post("/api/v1/planning/AAPL/activate-buy-today")
    assert activation.status_code == 403

    removed_from_buylist = command(authenticated, "remove_buylist", 3)
    assert removed_from_buylist.status_code == 200
    assert removed_from_buylist.json()["card"]["watchlist_member"] is True
    assert removed_from_buylist.json()["card"]["buylist_member"] is False
    assert removed_from_buylist.json()["card"]["breakout_price"] == 192.75
    assert authenticated.get("/api/v1/buy-today-drafts").json()["rows"] == []


def test_buy_today_cancellation_rejects_a_stale_revision(authenticated):
    assert command(authenticated, "add_watchlist", 0).status_code == 200
    assert command(authenticated, "set_breakout", 1, price=149).status_code == 200
    assert command(authenticated, "promote_buylist", 2).status_code == 200
    assert authenticated.post(
        "/api/v1/planning/AAPL/buy-today-preview",
        json={"expected_revision": 3},
    ).status_code == 200
    assert command(authenticated, "set_breakout", 3, price=150).status_code == 200

    stale = authenticated.request(
        "DELETE",
        "/api/v1/planning/AAPL/buy-today-preview",
        json={"expected_revision": 3},
    )
    assert stale.status_code == 409
    assert stale.json()["current"]["version"] == 4


def test_breakout_is_independent_and_buylist_requires_it(authenticated):
    saved = command(authenticated, "set_breakout", 0, price=192.75)
    assert saved.status_code == 200
    assert saved.json()["card"]["watchlist_member"] is False
    assert saved.json()["card"]["buylist_member"] is False
    assert saved.json()["card"]["display_stage"] == "BREAKOUT"

    promoted = command(authenticated, "promote_buylist", 1)
    assert promoted.status_code == 200
    assert promoted.json()["card"]["watchlist_member"] is False
    assert promoted.json()["card"]["buylist_member"] is True

    added = command(authenticated, "add_watchlist", 2)
    assert added.status_code == 200
    assert added.json()["card"]["watchlist_member"] is True
    assert added.json()["card"]["buylist_member"] is True


def test_removing_buylist_does_not_create_watchlist_membership(authenticated):
    saved = command(authenticated, "set_breakout", 0, price=192.75)
    assert saved.status_code == 200
    promoted = command(authenticated, "promote_buylist", 1)
    assert promoted.status_code == 200

    removed = command(authenticated, "remove_buylist", 2)

    assert removed.status_code == 200
    card = removed.json()["card"]
    assert card["watchlist_member"] is False
    assert card["buylist_member"] is False
    assert card["breakout_price"] == 192.75


def test_buylist_rejects_a_watchlist_without_breakout(authenticated):
    assert command(authenticated, "add_watchlist", 0).status_code == 200
    rejected = command(authenticated, "promote_buylist", 1)
    assert rejected.status_code == 409
    assert "breakout price" in rejected.json()["detail"]


def test_idempotency_and_changed_payload_rejection(authenticated):
    command_id = str(uuid.uuid4())
    first = command(authenticated, "add_watchlist", 0, command_id=command_id)
    second = command(authenticated, "add_watchlist", 0, command_id=command_id)
    assert first.status_code == second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    changed = command(
        authenticated, "set_breakout", 1, command_id=command_id, price=100
    )
    assert changed.status_code == 409
    assert "different payload" in changed.json()["detail"]


def test_stale_planning_revision_returns_current_state(authenticated):
    assert command(authenticated, "add_watchlist", 0).status_code == 200
    assert command(authenticated, "set_breakout", 1, price=100).status_code == 200
    stale = command(authenticated, "promote_buylist", 1)
    assert stale.status_code == 409
    assert stale.json()["state"] == "CONFLICT"
    assert stale.json()["current"]["version"] == 2


def test_drawing_create_edit_delete_and_tombstone(authenticated):
    payload = {
        "id": "line-aapl-1",
        "symbol": "AAPL",
        "start_date": "2026-09-01T13:30:00Z",
        "start_price": 100.0,
        "end_date": "2026-09-02T14:30:00Z",
        "end_price": 110.0,
        "timeframe": "1H",
    }
    created = authenticated.post("/api/v1/drawings", json=payload)
    assert created.status_code == 200
    drawing = created.json()["drawing"]
    assert drawing["revision"] == 1

    update_payload = {
        "expected_revision": 1,
        "start_date": payload["start_date"],
        "start_price": 101,
        "end_date": payload["end_date"],
        "end_price": 111,
        "timeframe": "1H",
    }
    updated = authenticated.put("/api/v1/drawings/line-aapl-1", json=update_payload)
    assert updated.status_code == 200
    assert updated.json()["drawing"]["revision"] == 2

    stale = authenticated.put("/api/v1/drawings/line-aapl-1", json=update_payload)
    assert stale.status_code == 409

    deleted = authenticated.request(
        "DELETE",
        "/api/v1/drawings/line-aapl-1",
        json={"expected_revision": 2},
    )
    assert deleted.status_code == 200
    assert deleted.json()["drawing"]["deleted"] is True
    assert authenticated.get("/api/v1/drawings/AAPL").json()["rows"] == []

    revived = authenticated.post("/api/v1/drawings", json=payload)
    assert revived.status_code == 409
    assert "cannot be revived" in revived.json()["detail"]


def test_drawing_does_not_change_breakout(authenticated):
    assert command(authenticated, "add_watchlist", 0).status_code == 200
    assert command(authenticated, "set_breakout", 1, price=155.5).status_code == 200
    response = authenticated.post(
        "/api/v1/drawings",
        json={
            "symbol": "AAPL",
            "start_date": "2026-09-01T13:30:00Z",
            "start_price": 10,
            "end_date": "2026-09-02T13:30:00Z",
            "end_price": 20,
            "timeframe": "1D",
        },
    )
    assert response.status_code == 200
    assert authenticated.get("/api/v1/planning/AAPL").json()["card"]["breakout_price"] == 155.5
