import datetime as dt

import pandas as pd
import pytest

from src.core.monitor_bar_repair import repair_zero_volume_minutes
from src.utils.market_calendar import US_MARKET_ZONE


def sample():
    index = pd.date_range('2026-10-29 09:30', periods=6, freq='min', tz=US_MARKET_ZONE)
    bars = pd.DataFrame({'Open':100.,'High':100.,'Low':100.,'Close':100.,'Volume':[100.,0.,0.,0.,100.,100.]},index=index)
    aggregate = pd.DataFrame({'Open':[100.],'High':[100.],'Low':[100.],'Close':[100.],'Volume':[200.]},index=index[:1])
    return bars.drop(index[1:4]), aggregate, index


def test_repair_requires_completed_matching_volume_and_prices_and_keeps_latest_quote():
    bars, aggregate, index = sample()
    repaired, count = repair_zero_volume_minutes(bars,aggregate,interval_minutes=5,now=index[-1].to_pydatetime())
    assert count == 3
    assert repaired.index.equals(index)
    assert repaired.loc[index[1:4], 'Volume'].eq(0).all()
    assert repaired.loc[index[1:4], 'Close'].eq(100).all()
    assert bars.index[-1] == repaired.index[-1]
    assert len(bars) == 3


@pytest.mark.parametrize('damage', ['volume','price','forming','invalid','opening','duplicate'])
def test_uncertain_gaps_are_never_silently_filled(damage):
    bars, aggregate, index = sample()
    now = index[-1].to_pydatetime()
    if damage == 'volume': aggregate.loc[index[0],'Volume'] += 1
    if damage == 'price': aggregate.loc[index[0],'High'] += .01
    if damage == 'forming': now = index[3].to_pydatetime()
    if damage == 'invalid': bars.loc[index[0],'Volume'] = float('nan')
    if damage == 'opening':
        bars = bars.drop(index[0])
        aggregate.loc[index[0],'Volume'] = 100
    if damage == 'duplicate': bars = pd.concat([bars,bars.iloc[:1]])
    repaired, count = repair_zero_volume_minutes(bars,aggregate,interval_minutes=5,now=now)
    if damage == 'opening':
        assert index[0] not in repaired.index
    else:
        assert count == 0
        assert len(repaired) == len(bars)


def test_premarket_price_cannot_be_used_to_invent_regular_open():
    bars, aggregate, index = sample()
    bars = bars.drop(index[0])
    bars.loc[index[0]-pd.Timedelta(minutes=1)] = [100,100,100,100,100]
    bars = bars.sort_index()
    aggregate.loc[index[0],'Volume'] = 100
    repaired, _ = repair_zero_volume_minutes(bars,aggregate,interval_minutes=5,now=index[-1].to_pydatetime())
    assert index[0] not in repaired.index
