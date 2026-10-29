import copy
import datetime as dt

from src.web.monitor_history import MonitorHistory, early_orb_review
from src.utils.market_calendar import US_MARKET_ZONE


def row():
    return {'symbol':'AAA','breakout_price':100.,'current_price':101.,'quote_status':'CURRENT',
            'adr_percent':4.,'orb':[{'window':'5m','price_status':'PASS','position_status':'PASS',
            'liquidity_status':'PASS','execution_price':101.,'low':99.,'score':90}],
            'momentum':{'quality':'ESTIMATED','as_of':'2026-10-29T09:36:00-04:00',
                        'vwap_distance_adr':.1,'assessment':{'label':'Confirm','reasons':['Estimated history']}}}


def test_early_review_is_independent_of_strong_and_never_activates_a_card():
    card=row(); original=copy.deepcopy(card)
    result=early_orb_review(card)
    assert result['active'] and result['data_review_required'] and result['advisory']
    assert card == original
    card['momentum']['vwap_distance_adr']=None
    assert not early_orb_review(card)['active']
    card['momentum']['vwap_distance_adr']=.1
    card['current_price']=105
    assert not early_orb_review(card)['active']
    card['current_price']=99
    assert not early_orb_review(card)['active']
    card['quote_status']='STALE'
    assert not early_orb_review(card)['active']


def test_live_history_survives_restart_extension_and_deduplicates(tmp_path):
    now=dt.datetime(2026,10,29,9,36,tzinfo=US_MARKET_ZONE)
    path=tmp_path/'history.sqlite3'; history=MonitorHistory(path)
    card=row();history.observe([card],now);history.observe([card],now)
    assert len(card['condition_history']['events'])==1
    assert card['condition_history']['first_early']['observed_at']==now.isoformat()
    card['current_price']=105
    card['momentum']['assessment']['label']='Extended'
    history=MonitorHistory(path);history.observe([card],now+dt.timedelta(minutes=5))
    assert not card['early_review']['active']
    assert card['condition_history']['first_early']
    assert card['condition_history']['first_extended']
    assert card['condition_history']['first_strong'] is None
    assert len(card['condition_history']['events'])==2
    card['breakout_price']=102
    history.observe([card],now+dt.timedelta(minutes=6))
    assert card['condition_history']['first_early'] is None
    history.observe([card],now+dt.timedelta(days=1))
    assert len(card['condition_history']['events'])==1


def test_background_monitor_runs_during_regular_session_without_a_browser():
    from src.web.intraday_monitor import IntradayMonitor, MonitorContext
    now=dt.datetime(2026,10,29,10,0,tzinfo=US_MARKET_ZONE)
    monitor=IntradayMonitor(lambda:MonitorContext(), enabled=True, now=lambda:now, monotonic=lambda:1)
    calls=[]
    def refresh():
        calls.append(True)
        monitor._stop.set()
        monitor._wake.set()
    monitor.refresh_once=refresh
    monitor._run()
    assert calls == [True]


def test_aggregate_backfill_is_batched_and_failed_symbols_are_not_retried_each_minute():
    import pandas as pd
    from src.web.intraday_monitor import IntradayMonitor, MonitorContext
    now=dt.datetime(2026,10,29,9,36,tzinfo=US_MARKET_ZONE)
    calls=[]
    class Provider:
        def fetch(self, symbols, *, interval, period=None):
            calls.append((symbols,interval))
            return {}
    monitor=IntradayMonitor(lambda:MonitorContext(),enabled=False,provider=Provider(),now=lambda:now,monotonic=lambda:0)
    index=pd.date_range('2026-10-29 09:30',periods=6,freq='min',tz=US_MARKET_ZONE).delete(2)
    frame=pd.DataFrame({'Open':100.,'High':100.,'Low':100.,'Close':100.,'Volume':100.},index=index)
    monitor._frames={f'A{i:03}':frame for i in range(30)}
    monitor._repair_minute_coverage(list(monitor._frames),now)
    assert [len(c[0]) for c in calls]==[24,24]
    failed=set(calls[0][0])
    monitor._repair_minute_coverage(list(monitor._frames),now)
    assert [len(c[0]) for c in calls]==[24,24,6,6]
    assert not failed.intersection(calls[2][0])
