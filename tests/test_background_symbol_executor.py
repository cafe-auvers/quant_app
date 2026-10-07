import threading

from src.services.background_symbol_executor import BackgroundSymbolExecutor


def test_slow_buy_does_not_delay_another_symbols_stop_and_same_card_is_serial():
    executor = BackgroundSymbolExecutor(ordinary_workers=1, protective_workers=1)
    buy_started, finish_buy, stop_executed = (threading.Event() for _ in range(3))
    next_same_card = threading.Event()
    def buy():
        buy_started.set()
        assert finish_buy.wait(3)
    try:
        executor.dispatch("BUY", buy)
        assert buy_started.wait(1)
        executor.dispatch("STOP", stop_executed.set, protective=True)
        assert stop_executed.wait(1)
        executor.dispatch("BUY", next_same_card.set, protective=True)
        assert not next_same_card.wait(0.05)
        finish_buy.set()
        assert next_same_card.wait(1)
    finally:
        finish_buy.set()
        executor.close()


def test_ordinary_updates_cannot_overwrite_a_queued_protective_exit():
    executor = BackgroundSymbolExecutor(ordinary_workers=1, protective_workers=1)
    started, finish, protected, wrong = (threading.Event() for _ in range(4))
    def first():
        started.set()
        assert finish.wait(3)
    try:
        executor.dispatch("S", first)
        assert started.wait(1)
        executor.dispatch("S", protected.set, protective=True)
        executor.dispatch("S", wrong.set)
        finish.set()
        assert protected.wait(1)
        assert not wrong.is_set()
    finally:
        finish.set()
        executor.close()
    assert not executor.dispatch("S", wrong.set)


def test_later_position_heartbeat_cannot_replace_a_latched_stop():
    executor = BackgroundSymbolExecutor(ordinary_workers=1, protective_workers=1)
    started, finish, stop, heartbeat = (threading.Event() for _ in range(4))
    def first():
        started.set()
        assert finish.wait(3)
    try:
        executor.dispatch("S", first, protective=True)
        assert started.wait(1)
        executor.dispatch("S", stop.set, protective=True, critical=True)
        executor.dispatch("S", heartbeat.set, protective=True)
        finish.set()
        assert stop.wait(1)
        assert not heartbeat.is_set()
    finally:
        finish.set()
        executor.close()


def test_queued_buy_is_promoted_to_protective_pool_before_it_starts():
    executor = BackgroundSymbolExecutor(ordinary_workers=1, protective_workers=1)
    started, finish, stop, unwanted_buy = (threading.Event() for _ in range(4))
    def blocker():
        started.set()
        assert finish.wait(3)
    try:
        executor.dispatch("OTHER", blocker)
        assert started.wait(1)
        executor.dispatch("S", unwanted_buy.set)
        executor.dispatch("S", stop.set, protective=True, critical=True)
        assert stop.wait(1)
        assert not finish.is_set()
        assert not unwanted_buy.is_set()
    finally:
        finish.set()
        executor.close()
