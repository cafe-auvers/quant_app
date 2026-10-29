"""Durable advisory observations; no planning or broker mutation authority."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import sqlite3
import threading
from contextlib import contextmanager

from src.core.intraday_momentum import best_passed_orb
from src.utils.market_calendar import US_MARKET_ZONE
from .intraday_monitor import positive


def early_orb_review(row: dict) -> dict:
    """Separate an observable early ORB from the stricter Strong assessment."""
    plan = best_passed_orb(row)
    momentum = row.get('momentum', {})
    price = positive(row.get('current_price'))
    entry = positive((plan or {}).get('execution_price'))
    breakout = positive(row.get('breakout_price'))
    adr = positive(row.get('adr_percent'))
    if row.get('quote_status') != 'CURRENT' or not plan or not all((price, entry, breakout, adr)):
        return {'active': False, 'reason': 'Waiting for fresh price, passing ORB and position inputs'}
    vwap_distance = momentum.get('vwap_distance_adr')
    if not isinstance(vwap_distance, (int,float)) or not math.isfinite(vwap_distance):
        return {'active': False, 'reason': 'Waiting for current VWAP context'}
    if price < breakout or price <= (positive(plan.get('low')) or 0):
        return {'active': False, 'reason': 'Price is below the breakout or ORB stop'}
    distance = (price/entry-1)*100/adr
    stretched = distance > .5 or distance < -.5 or vwap_distance > .5
    if stretched:
        return {'active': False, 'reason': 'Outside the early entry-distance bounds'}
    return {'active': True, 'reason': 'ORB price, position and liquidity passed; price is within 0.5 ADR of entry. Review condition and data quality.',
            'window': plan['window'], 'planned_entry': entry, 'observed_price': price,
            'data_review_required': momentum.get('quality') != 'FULL' or momentum.get('assessment', {}).get('label') in ('Check data', 'Confirm'),
            'advisory': True}


class MonitorHistory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('''CREATE TABLE IF NOT EXISTS monitor_observations (
                id INTEGER PRIMARY KEY, session TEXT NOT NULL, symbol TEXT NOT NULL,
                breakout REAL NOT NULL, observed_at TEXT NOT NULL, as_of TEXT,
                condition TEXT NOT NULL, early INTEGER NOT NULL, orb_passed INTEGER NOT NULL,
                payload TEXT NOT NULL)''')
            conn.execute('CREATE INDEX IF NOT EXISTS monitor_observation_scope ON monitor_observations(session,symbol,breakout,id)')
            conn.execute('''CREATE TABLE IF NOT EXISTS monitor_strong_replays (
                session TEXT NOT NULL, symbol TEXT NOT NULL, breakout REAL NOT NULL,
                request_key TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(session,symbol,breakout))''')

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def observe(self, rows: list[dict], now: dt.datetime) -> None:
        """Append actual transitions only; never backdate a first observation."""
        session = now.astimezone(US_MARKET_ZONE).date().isoformat()
        with self._lock, self._connection() as conn:
            for row in rows:
                breakout = positive(row.get('breakout_price'))
                if breakout is None:
                    continue
                scope = (session, row['symbol'], breakout)
                review = early_orb_review(row)
                row['early_review'] = review
                momentum = row.get('momentum', {})
                label = momentum.get('assessment', {}).get('label') or momentum.get('status', 'UNAVAILABLE')
                passed = bool(best_passed_orb(row)) and row.get('quote_status') == 'CURRENT'
                previous = conn.execute('SELECT condition,early,orb_passed FROM monitor_observations WHERE session=? AND symbol=? AND breakout=? ORDER BY id DESC LIMIT 1',scope).fetchone()
                signature = (label, int(review['active']), int(passed))
                if previous != signature:
                    payload = {'observed_at':now.isoformat(),'as_of':momentum.get('as_of'),
                               'condition':label,'early':review['active'],'orb_passed':passed,
                               'price':row.get('current_price'),'quality':momentum.get('quality'),
                               'reasons':momentum.get('assessment', {}).get('reasons', [momentum.get('reason')])}
                    conn.execute('INSERT INTO monitor_observations(session,symbol,breakout,observed_at,as_of,condition,early,orb_passed,payload) VALUES(?,?,?,?,?,?,?,?,?)',
                                 (*scope,now.isoformat(),momentum.get('as_of'),*signature,json.dumps(payload)))
                events = [json.loads(item[0]) for item in conn.execute('SELECT payload FROM monitor_observations WHERE session=? AND symbol=? AND breakout=? ORDER BY id',scope)]
                first = lambda predicate: next((e for e in events if predicate(e)),None)
                row['condition_history'] = {'session_date':session,'observed_from':events[0]['observed_at'] if events else None,
                    'first_orb':first(lambda e:e['orb_passed']), 'first_early':first(lambda e:e['early']),
                    'first_strong':first(lambda e:e['condition']=='Strong'),
                    'first_extended':first(lambda e:e['condition']=='Extended'),'events':events[-30:],
                    'source':'Live observations since monitoring started; no reconstructed past labels'}

    def load_replays(self, session):
        with self._lock, self._connection() as conn:
            return {(symbol, breakout): {'request_key': key, 'result': json.loads(payload)}
                    for symbol, breakout, key, payload in conn.execute(
                        'SELECT symbol,breakout,request_key,payload FROM monitor_strong_replays WHERE session=?', (session,))}

    def save_replay(self, scope, request_key, result):
        with self._lock, self._connection() as conn:
            conn.execute('INSERT OR REPLACE INTO monitor_strong_replays(session,symbol,breakout,request_key,payload) VALUES(?,?,?,?,?)',
                         (*scope, request_key, json.dumps(result)))
