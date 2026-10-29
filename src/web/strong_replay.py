"""Reconstruct missed advisory Strong readings without downloading or trading."""
from __future__ import annotations

from collections import OrderedDict
import datetime as dt
import hashlib
import json
import logging
import threading

import pandas as pd

from src.core.intraday_momentum import BENCHMARK, evaluate_momentum
from src.core.monitor_bar_repair import repair_zero_volume_minutes
from src.utils.market_calendar import US_MARKET_ZONE, nyse_regular_session_close_time
from .intraday_monitor import MonitorContext, evaluate_monitor_row, positive


logger = logging.getLogger(__name__)
REPLAY_VERSION = 'STRONG_REPLAY_V1'
ASSUMPTIONS = ('Reconstructed from currently available Yahoo bars using the saved breakout, '
               'ORB settings and captured account-equity basis. Historical edits and actual '
               'screen labels are unknown. Completed aggregate proofs are admitted only '
               'after their intervals close. Advisory review only.')
COLUMNS = ['Open', 'High', 'Low', 'Close', 'Volume']


def _frame(frame, through):
    if (frame is None or frame.empty or not isinstance(frame.index, pd.DatetimeIndex)
            or frame.index.tz is None or frame.index.has_duplicates
            or not set(COLUMNS) <= set(frame)):
        return pd.DataFrame()
    frame = frame.copy()
    frame.index = frame.index.tz_convert(US_MARKET_ZONE)
    return frame.loc[frame.index < through].sort_index()


def _ends(frame, interval):
    return pd.DatetimeIndex([
        min(stamp + pd.Timedelta(minutes=interval), pd.Timestamp(dt.datetime.combine(
            stamp.date(), nyse_regular_session_close_time(stamp.date()), US_MARKET_ZONE)))
        for stamp in frame.index
    ])


def _candidate_minutes(frame, start, through):
    """An inexpensive search hint; carried prices never establish a Strong result."""
    if start not in frame.index or positive(frame.loc[start, 'Open']) is None:
        return None
    grid = pd.date_range(start, through-pd.Timedelta(minutes=1), freq='min')
    closes = pd.to_numeric(frame.Close, errors='coerce').reindex(grid).ffill()
    opened = float(frame.loc[start, 'Open'])
    travel = closes.diff().abs()
    if travel.empty:
        return grid[:0]
    travel.iloc[0] = abs(closes.iloc[0]-opened)
    efficiency = (closes-opened)/travel.cumsum().replace(0, float('nan'))
    # Leave a rounding margin in search only; the final evaluator is exact.
    return grid[(efficiency >= .4-1e-10) & (grid >= start+pd.Timedelta(minutes=4))]


def replay_strong(job, cancelled=lambda: False):
    """Use only completed, causally available bars for each candidate minute."""
    through = pd.Timestamp(job['through']).tz_convert(US_MARKET_ZONE)
    day = through.date()
    start = pd.Timestamp(dt.datetime.combine(day, dt.time(9, 30), US_MARKET_ZONE))
    raw = job['frames']
    stock_symbol = job['card']['symbol']
    sector = job['card']['sector_symbol']
    result = {'status': 'COMPLETE', 'kind': 'RECONSTRUCTED', 'version': REPLAY_VERSION,
              'session_date': day.isoformat(), 'checked_through': through.isoformat(),
              'calculated_at': job['calculated_at'], 'assumptions': ASSUMPTIONS,
              'context_key': job['context_key'], 'basis_equity': job['equity'],
              'first_strong': None, 'last_strong': None, 'strong_minutes': 0,
              'intervals': [], 'uncertain_candidates': 0}
    candidates = _candidate_minutes(raw[stock_symbol], start, through)
    if candidates is None:
        return {**result, 'status': 'UNAVAILABLE', 'reason': 'A verified regular-session opening price is unavailable'}
    if any(start not in raw[symbol].index for symbol in (BENCHMARK, sector)):
        return {**result, 'status': 'UNAVAILABLE', 'reason': 'SPY or sector opening data unavailable'}
    repaired = dict(raw)
    for symbol in repaired:
        for interval in (5, 60):
            repaired[symbol], _ = repair_zero_volume_minutes(
                repaired[symbol], job['aggregates'][interval][symbol],
                interval_minutes=interval, now=start.to_pydatetime())
    previous = start
    context = MonitorContext(rows=[job['card']], settings=job['settings'], equity=job['equity'])
    for cutoff in candidates:
        if cancelled():
            return None
        available = cutoff+pd.Timedelta(minutes=1)
        evaluated = (available+pd.Timedelta(seconds=1)).to_pydatetime()
        for symbol in repaired:
            for interval in (5, 60):
                aggregate = job['aggregates'][interval][symbol]
                if aggregate.empty:
                    continue
                ends = _ends(aggregate, interval)
                fresh = aggregate[(aggregate.index.date == day) & (ends > previous) & (ends <= available)]
                if not fresh.empty:
                    repaired[symbol], _ = repair_zero_volume_minutes(
                        repaired[symbol], fresh, interval_minutes=interval, now=evaluated)
        previous = available
        clipped = {}
        for symbol, frame in repaired.items():
            # A repaired flat minute must not make an old source quote newer.
            observed = raw[symbol].index[raw[symbol].index <= cutoff]
            clipped[symbol] = frame.loc[:min(cutoff, observed[-1])] if len(observed) else pd.DataFrame()
        row = evaluate_monitor_row(job['card'], clipped[stock_symbol], now=evaluated,
                                   context=context, adr=job['adr'])
        momentum = evaluate_momentum(row, clipped[stock_symbol], clipped[BENCHMARK],
                                    now=evaluated, adr=job['adr'], sector_benchmark=clipped[sector],
                                    sector_symbol=sector, sector_name=job['card'].get('sector_name'))
        if momentum.get('quality') != 'FULL':
            result['uncertain_candidates'] += 1
        if momentum.get('assessment', {}).get('label') != 'Strong' or momentum.get('quality') != 'FULL':
            continue
        event = {'observed_at': available.isoformat(), 'kind': 'RECONSTRUCTED',
                 'condition': 'Strong', 'signal_as_of': momentum['as_of'],
                 'quality': momentum['quality'], 'price': momentum.get('signal_price'),
                 'er': momentum.get('directional_efficiency'), 'rvol': momentum.get('rvol'),
                 'entry_window': momentum.get('entry_window')}
        result['first_strong'] = result['first_strong'] or event
        result['last_strong'] = event
        result['strong_minutes'] += 1
        intervals = result['intervals']
        if intervals and available-pd.Timestamp(intervals[-1]['to']) == pd.Timedelta(minutes=1):
            intervals[-1]['to'] = available.isoformat()
            intervals[-1]['minutes'] += 1
        else:
            intervals.append({'from': available.isoformat(), 'to': available.isoformat(), 'minutes': 1})
    return result


class StrongReplayWorker:
    """One bounded worker; quote refreshes only queue immutable cached inputs."""
    def __init__(self, history, on_result):
        self.history = history
        self.on_result = on_result
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._running = None
        self._stop = threading.Event()
        self._thread = None
        self._basis = {}
        self._through = {}

    def attach_and_queue(self, rows, context, frames, aggregates, adr, now):
        now = now.astimezone(US_MARKET_ZONE)
        session = now.date().isoformat()
        saved = self.history.load_replays(session)
        active = set()
        ordered = sorted(rows, key=lambda row: (not row.get('buy_today_member'), not bool(row.get('orb')), row['symbol']))
        for row in ordered:
            history = row.get('condition_history')
            if not history or not history.get('observed_from'):
                continue
            scope = (session, row['symbol'], row['breakout_price'])
            active.add(scope)
            session_open = pd.Timestamp(dt.datetime.combine(now.astimezone(US_MARKET_ZONE).date(), dt.time(9, 30), US_MARKET_ZONE))
            if pd.Timestamp(now) < session_open+pd.Timedelta(minutes=5):
                history['reconstruction'] = {'status': 'WAITING', 'reason': 'Waiting for five regular-session minutes'}
                continue
            prior = saved.get((row['symbol'], row['breakout_price']), {})
            replay_found = bool(prior.get('result', {}).get('first_strong'))
            initial = pd.Timestamp(prior['result']['checked_through']).tz_convert(US_MARKET_ZONE) if replay_found else pd.Timestamp(now).floor('min')
            through = self._through.setdefault(scope, initial)
            live_first = history.get('first_strong')
            if not replay_found:
                # Revisit unqualified history periodically as aggregate evidence
                # arrives, while live observations continue every minute.
                through = max(through, pd.Timestamp(now).floor('15min'))
            if live_first:
                through = min(through, pd.Timestamp(live_first['observed_at']).tz_convert(US_MARKET_ZONE).floor('min'))
            through = min(through, pd.Timestamp(dt.datetime.combine(through.date(), nyse_regular_session_close_time(through.date()), US_MARKET_ZONE)))
            self._through[scope] = through
            symbols = list(dict.fromkeys((row['symbol'], BENCHMARK, row.get('sector_symbol'))))
            available = {symbol: _frame(frames.get(symbol), through) for symbol in symbols if symbol}
            if (context.settings is None or positive(adr.get(row['symbol'])) is None
                    or len(available) != len(symbols) or any(frame.empty for frame in available.values())):
                history['reconstruction'] = {'status': 'UNAVAILABLE', 'reason': 'Waiting for stock, SPY, sector, ADR and ORB settings'}
                continue
            descriptor = {'version': REPLAY_VERSION, 'session': session, 'symbol': row['symbol'],
                          'breakout': row['breakout_price'], 'sector': row['sector_symbol'],
                          'is_ep': row.get('is_ep'), 'settings': context.settings.to_dict(),
                          'adr': adr[row['symbol']], 'through': through.isoformat()}
            context_key = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
            basis_key = (*scope, context_key)
            equity = prior.get('result', {}).get('basis_equity') if prior.get('result', {}).get('context_key') == context_key else self._basis.get(basis_key, context.equity)
            if positive(equity) is None:
                history['reconstruction'] = {'status': 'UNAVAILABLE', 'reason': 'Captured account-equity basis unavailable'}
                continue
            self._basis[basis_key] = equity
            proof = {interval: {} for interval in (5, 60)}
            digest = hashlib.sha256((context_key+str(equity)).encode())
            for symbol, frame in available.items():
                digest.update(symbol.encode())
                digest.update(pd.util.hash_pandas_object(frame[COLUMNS], index=True).values.tobytes())
                for interval in (5, 60):
                    aggregate = _frame(aggregates.get(interval, {}).get(symbol), through)
                    if not aggregate.empty:
                        aggregate = aggregate[_ends(aggregate, interval) <= through]
                    proof[interval][symbol] = aggregate
                    digest.update(str(interval).encode())
                    if not aggregate.empty:
                        digest.update(pd.util.hash_pandas_object(aggregate[COLUMNS], index=True).values.tobytes())
            key = digest.hexdigest()
            if prior.get('request_key') == key:
                history['reconstruction'] = {**prior['result'], 'request_key': key}
                with self._condition:
                    self._pending.pop(scope, None)
                continue
            history['reconstruction'] = {'status': 'CHECKING', 'request_key': key, 'checked_through': through.isoformat()}
            job = {'scope': scope, 'request_key': key, 'context_key': context_key,
                   'card': {key: row.get(key) for key in ('symbol', 'breakout_price', 'is_ep', 'sector_symbol', 'sector_name')},
                   'through': through.isoformat(), 'calculated_at': now.isoformat(),
                   'settings': context.settings, 'equity': equity, 'adr': adr[row['symbol']],
                   'frames': available, 'aggregates': proof}
            with self._condition:
                if self._running != (scope, key):
                    self._pending[scope] = job
        with self._condition:
            self._pending = OrderedDict((scope, job) for scope, job in self._pending.items() if scope in active)
            self._condition.notify_all()

    def start(self):
        with self._condition:
            if self._pending and self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._run, name='web-strong-replay', daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _run(self):
        while not self._stop.is_set():
            with self._condition:
                if not self._pending:
                    self._condition.wait(timeout=1)
                    continue
                scope, job = self._pending.popitem(last=False)
                self._running = (scope, job['request_key'])
            try:
                result = replay_strong(job, self._stop.is_set)
                if result is not None and not self._stop.is_set():
                    self.history.save_replay(scope, job['request_key'], result)
                    self.on_result(scope, job['request_key'], result)
            except Exception:
                logger.warning('Strong reconstruction failed for %s', scope[1], exc_info=True)
                if not self._stop.is_set():
                    self.on_result(scope, job['request_key'], {'status': 'UNAVAILABLE', 'reason': 'Historical calculation unavailable; retrying on the next refresh'})
            finally:
                with self._condition:
                    self._running = None

    def close(self):
        self._stop.set()
        with self._condition:
            self._pending.clear()
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=1)
