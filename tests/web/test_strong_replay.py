import copy
import datetime as dt
import threading

import pandas as pd
import pytest

from src.risk.orb_position import OrbSettings
from src.utils.market_calendar import US_MARKET_ZONE
from src.web.intraday_monitor import IntradayMonitor, MonitorContext
from src.web.monitor_history import MonitorHistory
from src.web.strong_replay import StrongReplayWorker, replay_strong


@pytest.fixture
def replay_inputs(tmp_path):
    day = dt.date(2026, 10, 29)
    now = dt.datetime.combine(day, dt.time(9, 42), US_MARKET_ZONE)
    frames = {}
    for symbol in ('AAA', 'SPY', 'SECTOR'):
        sessions = []
        for session in (dt.date(2026, 10, 23), dt.date(2026, 10, 26), dt.date(2026, 10, 27), dt.date(2026, 10, 28), day):
            current = session == day
            index = pd.date_range(dt.datetime.combine(session, dt.time(9, 30), US_MARKET_ZONE), periods=12 if current else 20, freq='min')
            close = [100.5+.1*i for i in range(len(index))] if symbol == 'AAA' and current else [100.]*len(index)
            sessions.append(pd.DataFrame({'Open': 100., 'High': [value+.05 for value in close],
                                          'Low': 98., 'Close': close,
                                          'Volume': 2000. if current else 1000.}, index=index))
        frames[symbol] = pd.concat(sessions)
    card = {'symbol': 'AAA', 'breakout_price': 100., 'sector_symbol': 'SECTOR',
            'sector_name': 'Example sector', 'quote_status': 'CURRENT', 'current_price': 101.6,
            'orb': [], 'momentum': {'assessment': {'label': 'Weak'}}}
    context = MonitorContext(rows=[card], settings=OrbSettings(), equity=100_000)
    aggregates = {5: {}, 60: {}}
    history = MonitorHistory(tmp_path/'history.sqlite3')
    history.observe([card], now)
    worker = StrongReplayWorker(history, lambda *_: None)
    worker.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, now)
    job = next(iter(worker._pending.values()))
    return card, context, frames, aggregates, history, worker, job, now


def test_completed_replay_finds_morning_strong_and_preserves_actual_live_log(replay_inputs):
    card, context, frames, aggregates, history, worker, job, now = replay_inputs
    original = {symbol: frame.copy() for symbol, frame in frames.items()}
    result = replay_strong(job)
    assert result['first_strong']['observed_at'] == '2026-10-29T09:35:00-04:00'
    assert result['first_strong']['kind'] == 'RECONSTRUCTED'
    assert result['first_strong']['quality'] == 'FULL'
    assert result['strong_minutes'] > 0
    assert result['checked_through'] == now.isoformat()
    history.save_replay(job['scope'], job['request_key'], result)
    restarted = StrongReplayWorker(MonitorHistory(history.path), lambda *_: None)
    restarted.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, now)
    assert not restarted._pending
    assert card['condition_history']['first_strong'] is None
    assert all(event['condition'] != 'Strong' for event in card['condition_history']['events'])
    assert card['condition_history']['reconstruction']['first_strong'] == result['first_strong']
    for symbol, frame in frames.items():
        pd.testing.assert_frame_equal(frame, original[symbol])


def test_missing_current_minutes_require_completed_aggregate_proof(replay_inputs):
    card, context, frames, aggregates, history, worker, _, now = replay_inputs
    frame = frames['AAA'].copy()
    missing = pd.Timestamp('2026-10-29T09:36:00-04:00')
    frame.loc[missing, ['Open', 'High', 'Low', 'Close']] = [100.5, 100.5, 100.5, 100.5]
    frame.loc[missing, 'Volume'] = 0
    block = frame.loc['2026-10-29T09:35:00-04:00':'2026-10-29T09:39:00-04:00']
    proof = pd.DataFrame({'Open': [block.Open.iloc[0]], 'High': [block.High.max()],
                          'Low': [block.Low.min()], 'Close': [block.Close.iloc[-1]],
                          'Volume': [block.Volume.sum()]}, index=block.index[:1])
    frames['AAA'] = frame.drop(missing)
    aggregates[5]['AAA'] = proof
    worker.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, now)
    job = next(iter(worker._pending.values()))
    result = replay_strong(job)
    # The 09:35 aggregate closes at 09:40. It cannot establish earlier readings.
    assert all(not ('2026-10-29T09:38' <= event['from'][:16] < '2026-10-29T09:40') for event in result['intervals'])
    assert any(event['from'] == '2026-10-29T09:40:00-04:00' for event in result['intervals'])
    damaged = copy.deepcopy(job)
    damaged['aggregates'][5]['AAA'].iloc[0, 4] += 1
    without_proof = replay_strong(damaged)
    assert all(event['to'] < '2026-10-29T09:38:00-04:00' for event in without_proof['intervals'])


def test_prior_volume_proof_can_restore_a_previously_uncertain_strong_reading(replay_inputs):
    card, context, frames, aggregates, history, worker, _, now = replay_inputs
    frame = frames['AAA'].copy()
    missing = pd.Timestamp('2026-10-28T09:32:00-04:00')
    frame.loc[missing, 'Volume'] = 0
    block = frame.loc['2026-10-28T09:30:00-04:00':'2026-10-28T09:34:00-04:00']
    proof = pd.DataFrame({'Open': [100.], 'High': [block.High.max()], 'Low': [98.],
                          'Close': [100.], 'Volume': [block.Volume.sum()]}, index=block.index[:1])
    frames['AAA'] = frame.drop(missing)
    worker.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, now)
    job = next(iter(worker._pending.values()))
    uncertain = replay_strong(job)
    assert uncertain['first_strong'] is None
    assert uncertain['uncertain_candidates'] > 0
    history.save_replay(job['scope'], job['request_key'], uncertain)
    aggregates[5]['AAA'] = proof
    worker.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, now)
    updated = next(iter(worker._pending.values()))
    assert updated['request_key'] != job['request_key']
    assert replay_strong(updated)['first_strong']['observed_at'] == '2026-10-29T09:35:00-04:00'


def test_threshold_candidate_still_uses_the_exact_final_assessment(replay_inputs):
    _, _, _, _, _, _, job, _ = replay_inputs
    frame = job['frames']['AAA'].copy()
    stamps = pd.date_range('2026-10-29 09:30', periods=5, freq='min', tz=US_MARKET_ZONE)
    frame.loc[stamps, 'Close'] = [101, 100, 101, 100.5, 102]
    frame.loc[stamps, 'High'] = [101.05, 100.05, 101.05, 100.55, 102.05]
    job['frames']['AAA'] = frame
    job['through'] = '2026-10-29T09:35:00-04:00'
    result = replay_strong(job)
    assert result['first_strong']['er'] == pytest.approx(.4)


def test_unqualified_history_revisits_later_cached_minutes_but_found_strong_does_not_replay_forever(replay_inputs):
    card, context, frames, aggregates, history, worker, job, now = replay_inputs
    history.save_replay(job['scope'], job['request_key'], {
        'status': 'COMPLETE', 'context_key': job['context_key'], 'basis_equity': job['equity'],
        'checked_through': job['through'], 'first_strong': None,
    })
    worker.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, now+dt.timedelta(minutes=4))
    later = next(iter(worker._pending.values()))
    assert later['through'] == '2026-10-29T09:45:00-04:00'
    result = replay_strong(later)
    history.save_replay(later['scope'], later['request_key'], result)
    worker.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, now+dt.timedelta(minutes=30))
    assert not worker._pending
    assert card['condition_history']['reconstruction']['first_strong']


def test_utc_next_day_still_uses_the_new_york_session(replay_inputs):
    card, context, frames, aggregates, history, _, _, now = replay_inputs
    worker = StrongReplayWorker(history, lambda *_: None)
    late = now.replace(hour=21).astimezone(dt.timezone.utc)
    assert late.date() == dt.date(2026, 10, 30)
    worker.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, late)
    job = next(iter(worker._pending.values()))
    assert job['scope'][0] == '2026-10-29'
    assert job['through'] == '2026-10-29T16:00:00-04:00'


def test_changed_evidence_settings_and_breakout_invalidate_replay(replay_inputs):
    card, context, frames, aggregates, history, worker, job, now = replay_inputs
    result = replay_strong(job)
    history.save_replay(job['scope'], job['request_key'], result)
    frames['AAA'] = frames['AAA'].copy()
    frames['AAA'].iloc[0, 4] += 1
    worker.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, now)
    assert card['condition_history']['reconstruction']['status'] == 'CHECKING'
    assert card['condition_history']['reconstruction']['request_key'] != job['request_key']
    context.settings = OrbSettings(opening_min_shares_per_minute=250)
    worker.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, now)
    assert next(iter(worker._pending.values()))['context_key'] != job['context_key']
    card['breakout_price'] = 100.1
    history.observe([card], now)
    worker.attach_and_queue([card], context, frames, aggregates, {'AAA': 10.}, now)
    assert card['condition_history']['first_strong'] is None
    assert card['condition_history']['reconstruction']['status'] == 'CHECKING'


def test_late_replay_result_cannot_attach_to_a_changed_scope(replay_inputs):
    card, _, _, _, _, _, job, _ = replay_inputs
    monitor = IntradayMonitor(lambda: MonitorContext(), enabled=False)
    monitor._snapshot['rows'] = [card]
    result = {'status': 'COMPLETE', 'first_strong': {'kind': 'RECONSTRUCTED'}}
    monitor._attach_replay_result(job['scope'], 'old request', result)
    assert card['condition_history']['reconstruction']['status'] == 'CHECKING'
    monitor._attach_replay_result((job['scope'][0], 'AAA', 101.), job['request_key'], result)
    assert card['condition_history']['reconstruction']['status'] == 'CHECKING'
    monitor._attach_replay_result(job['scope'], job['request_key'], result)
    assert card['condition_history']['reconstruction']['status'] == 'COMPLETE'
    monitor.close()


def test_one_background_replay_worker_stops_without_recording_incomplete_work(replay_inputs, monkeypatch):
    _, _, _, _, history, worker, _, _ = replay_inputs
    entered, release = threading.Event(), threading.Event()
    calls = []
    def blocked(_job, cancelled):
        calls.append(True)
        entered.set()
        release.wait(2)
        return None if cancelled() else {'status': 'COMPLETE'}
    monkeypatch.setattr('src.web.strong_replay.replay_strong', blocked)
    worker.start()
    assert entered.wait(2)
    worker.start()
    worker.close()
    release.set()
    worker._thread.join(2)
    assert calls == [True]
    assert not history.load_replays('2026-10-29')
