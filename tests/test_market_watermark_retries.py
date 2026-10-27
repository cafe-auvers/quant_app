import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from src.infrastructure.database.repositories import market_watermarks as wm


@pytest.mark.parametrize('persistent', [False, True])
def test_daily_verification_reconnects_once_and_preserves_failure(monkeypatch, persistent):
    connections = []

    class Connection:
        closed = False

        def execute(self, statement):
            if persistent or self is connections[0]:
                raise OperationalError('SELECT', {}, RuntimeError('timeout'))
            return SimpleNamespace(all=lambda: [SimpleNamespace(symbol='SPY', latest_date=dt.datetime(2026, 10, 27))])

        def close(self):
            self.closed = True

    class Engine:
        def connect(self):
            connection = Connection()
            connections.append(connection)
            return connection

    monkeypatch.setattr(wm, '_ensure_price_history_table', lambda engine: None)
    if persistent:
        with pytest.raises(RuntimeError, match='Unable to verify'):
            wm.get_latest_price_history_dates(Engine(), ['SPY'], strict=True)
    else:
        assert wm.get_latest_price_history_dates(Engine(), ['SPY'], strict=True) == {'SPY': dt.datetime(2026, 10, 27)}
    assert len(connections) == 2
    assert all(connection.closed for connection in connections)
