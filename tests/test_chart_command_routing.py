from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.board_workflow import (
    ActivateForToday,
    BoardCardProjection,
    CancelEntry,
    ClearBreakoutPrice,
    SetBreakoutPrice,
)
from src.core.trade_card_state import BoardStatus, TradeCardState
from src.ui.buyboard.controller import (
    _action_context,
    _route_live_command_via_operator_queue,
)
from src.ui.mixins.chart_command_routing_mixin import ChartCommandRoutingMixin


class _ForbiddenLegacyState:
    def __getattr__(self, name):
        raise AssertionError(f"retired compatibility state was accessed: {name}")


class _Window(ChartCommandRoutingMixin):
    def __init__(self, projections=()):
        self._buyboard_current_projections = tuple(projections)
        self.watchlist = _ForbiddenLegacyState()
        self.buylist_manager = _ForbiddenLegacyState()
        self.commands = []
        self.synced_targets = []
        self.logs = []
        self.reset_count = 0
        self.stale_symbols = []

    def _selected_dashboard_kis_profile(self):
        return {"environment": "PROD", "account_no": "1"}

    def _buyboard_dispatch_command(self, command, *, interaction_fingerprint=""):
        self.commands.append((command, interaction_fingerprint))
        return True

    def _buyboard_orb_buffer_pct(self):
        return 0.005

    def _sync_tradingview_target_price(self, symbol, price):
        self.synced_targets.append((symbol, price))

    def _reset_chart_mode_buttons(self):
        self.reset_count += 1

    def refresh_other_chart_views_for_symbol(self, symbol):
        self.stale_symbols.append(symbol)

    def append_log(self, message):
        self.logs.append(message)


def _projection(
    *,
    status=BoardStatus.BUYLIST,
    target=197.71,
    version=7,
    account="1",
    broker_quantity=0,
):
    return BoardCardProjection(
        card=TradeCardState(
            environment="PROD",
            account_no=account,
            symbol="WEX",
            version=version,
            board_status=status,
            buylist_member=status == BoardStatus.BUYLIST,
            breakout_price=target,
            broker_quantity=broker_quantity,
        ),
        readiness_generation=3,
        ownership_owner="KANBAN",
        ownership_version=4,
        strategy_instance_id="strategy-1",
    )


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    messages = []
    monkeypatch.setattr(
        "src.ui.mixins.chart_command_routing_mixin.QMessageBox.information",
        lambda *args: messages.append(("information", args[-1])),
    )
    monkeypatch.setattr(
        "src.ui.mixins.chart_command_routing_mixin.QMessageBox.warning",
        lambda *args: messages.append(("warning", args[-1])),
    )
    return messages


def test_set_existing_target_dispatches_exact_canonical_card_without_legacy_state():
    window = _Window([_projection()])

    window.set_chart_target_price("wex", 198.25)

    command, fingerprint = window.commands.pop()
    assert isinstance(command, SetBreakoutPrice)
    assert (command.environment, command.account_no, command.symbol) == (
        "PROD",
        "1",
        "WEX",
    )
    assert command.expected_card_version == 7
    assert command.expected_readiness_generation == 3
    assert command.expected_ownership_version == 4
    assert command.expected_strategy_instance_id == "strategy-1"
    assert command.price == 198.25
    assert command.buffer_pct == pytest.approx(0.005)
    assert fingerprint
    assert window.synced_targets == [("WEX", 198.25)]


def test_set_new_symbol_dispatches_version_zero_create_for_selected_account():
    window = _Window()

    window.set_chart_target_price("wex", 197.71)

    command, fingerprint = window.commands.pop()
    assert isinstance(command, SetBreakoutPrice)
    assert (command.environment, command.account_no, command.symbol) == (
        "PROD",
        "1",
        "WEX",
    )
    assert command.expected_card_version == 0
    assert command.price == 197.71
    assert command.buffer_pct == pytest.approx(0.005)
    assert fingerprint == ""


def test_clear_dispatches_canonical_command_and_never_mutates_local_mirrors():
    window = _Window([_projection(status=BoardStatus.BUY_TODAY)])

    window.clear_chart_target_price("WEX")

    command, fingerprint = window.commands.pop()
    assert isinstance(command, ClearBreakoutPrice)
    assert command.expected_card_version == 7
    assert fingerprint
    assert window.synced_targets == [("WEX", None)]
    assert "requested" in window.logs[-1].lower()


def test_queue_toggle_clears_passive_plan_or_deactivates_today_canonically():
    passive = _Window([_projection(status=BoardStatus.BUYLIST)])
    active = _Window([_projection(status=BoardStatus.BUY_TODAY)])

    passive._chart_queue_toggle("WEX")
    active._chart_queue_toggle("WEX")

    assert isinstance(passive.commands[0][0], ClearBreakoutPrice)
    assert isinstance(active.commands[0][0], CancelEntry)


def test_activate_toggle_uses_only_canonical_card_and_exact_fences():
    window = _Window([_projection(status=BoardStatus.BUYLIST)])

    window._chart_activate_toggle("WEX")

    command, fingerprint = window.commands.pop()
    assert isinstance(command, ActivateForToday)
    assert command.expected_card_version == 7
    assert command.account_no == "1"
    assert fingerprint


class _LegacyItem:
    symbol = "WEX"
    name = "WEX"
    breakout_price = 999.0


class _LegacyWatchlist:
    items = (_LegacyItem(),)

    def get(self, symbol):
        return self.items[0] if symbol == "WEX" else None


class _RenderProbe:
    def load_tradingview_chart(self):
        return self.watchlist.get("WEX").breakout_price

    def plot_intraday_watchlist_symbol(self):
        return self.watchlist.get("WEX").breakout_price


class _RenderWindow(ChartCommandRoutingMixin, _RenderProbe):
    def __init__(self):
        self.watchlist = _LegacyWatchlist()
        self._buyboard_current_projections = (_projection(),)

    def _selected_dashboard_kis_profile(self):
        return {"environment": "PROD", "account_no": "1"}


def test_all_chart_renderers_mask_stale_legacy_target_with_canonical_target():
    window = _RenderWindow()
    original = window.watchlist

    assert window.load_tradingview_chart() == 197.71
    assert window.plot_intraday_watchlist_symbol() == 197.71
    assert window.watchlist is original


def test_offline_passive_watchlist_target_remains_visible_without_canonical_card():
    window = _RenderWindow()
    window._buyboard_current_projections = ()

    assert window.load_tradingview_chart() == 999.0


def test_action_context_propagates_cached_operator_control_without_runtime():
    command = SetBreakoutPrice(
        environment="PROD",
        account_no="1",
        symbol="WEX",
        expected_card_version=0,
        price=197.71,
    )
    window = SimpleNamespace()
    window._has_cached_local_operator_control = lambda: True

    context = _action_context(window, command)

    assert context.local_operator_control is True
    assert context.action_ready is False


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("same", False),
        ("split", True),
        ("locked", True),
        ("unknown", True),
    ],
)
def test_live_command_queue_follows_operator_executor_topology(mode, expected):
    window = SimpleNamespace(
        _operator_executor_sync_mode=lambda: mode,
        _has_cached_local_operator_control=lambda: mode == "same",
    )

    assert _route_live_command_via_operator_queue(window) is expected


def test_same_device_command_stays_queued_without_fresh_operator_proof():
    window = SimpleNamespace(
        _operator_executor_sync_mode=lambda: "same",
        _has_cached_local_operator_control=lambda: False,
    )

    assert _route_live_command_via_operator_queue(window) is True


class _CompletionProbe:
    def __init__(self, projections=()):
        self._buyboard_current_projections = tuple(projections)
        self.synced_targets = []

    def _selected_dashboard_kis_profile(self):
        return {"environment": "PROD", "account_no": "1"}

    def _sync_tradingview_target_price(self, symbol, price):
        self.synced_targets.append((symbol, price))

    def _on_buyboard_command_completed(self, result):
        self.base_completion_result = result


class _CompletionWindow(ChartCommandRoutingMixin, _CompletionProbe):
    pass


def test_failed_clear_immediately_restores_the_canonical_chart_target():
    window = _CompletionWindow([_projection(target=197.71)])
    command = ClearBreakoutPrice(
        environment="PROD",
        account_no="1",
        symbol="WEX",
        expected_card_version=7,
    )
    result = SimpleNamespace(
        succeeded=False,
        request=SimpleNamespace(command=command),
    )

    window._on_buyboard_command_completed(result)

    assert window.base_completion_result is result
    assert window.synced_targets == [("WEX", 197.71)]
