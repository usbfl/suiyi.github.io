"""Live quotes must keep working while replay accounts remain immutable."""
import asyncio
import copy
from datetime import datetime
import httpx
import pytest
from app.config import INDEX, CST
from app.main import app
from app.market_monitor import MarketMonitor


async def test_live_quotes_are_independent_of_completed_or_active_replay(arena,dataset):
    await arena.new('replay',dataset['dates'][10],end=dataset['dates'][12])
    before=copy.deepcopy(arena.state)
    live=await arena.market_monitor.refresh(force=True)
    assert next(q for q in live['quotes'] if q['symbol']==INDEX)['date']==dataset['dates'][-1]
    assert arena.state==before
    assert arena.agents.calls==0


async def test_concurrent_views_share_one_provider_request(arena,monkeypatch):
    monitor=arena.market_monitor
    await monitor.close()
    monitor.last_attempt=-float('inf')
    original=arena.market.quotes
    calls=[]
    async def quotes(symbols):
        calls.append(symbols)
        await asyncio.sleep(.01)
        return await original(symbols)
    monkeypatch.setattr(arena.market,'quotes',quotes)
    await asyncio.gather(*(monitor.refresh() for _ in range(6)))
    assert len(calls)==1


async def test_failed_provider_keeps_original_quote_time_and_prices(arena,monkeypatch):
    monitor=arena.market_monitor
    first=await monitor.refresh(force=True)
    async def fail(symbols):raise RuntimeError('upstream unavailable')
    monkeypatch.setattr(arena.market,'quotes',fail)
    second=await monitor.refresh(force=True)
    assert second['quotes']==first['quotes']
    assert second['error'] and second['unavailable_symbols']
    assert second['revision']>first['revision']


async def test_samples_only_advance_for_actual_provider_ticks(arena,monkeypatch):
    monitor=arena.market_monitor
    await monitor.refresh(force=True)
    original=arena.market.quotes
    before=copy.deepcopy(monitor.samples[INDEX])
    await monitor.refresh(force=True)
    assert monitor.samples[INDEX]==before
    async def changed(symbols):
        result=await original(symbols)
        result[INDEX].update(price=12.5,time='2026-10-14T09:35:00+08:00',date='2026-10-14')
        return result
    monkeypatch.setattr(arena.market,'quotes',changed)
    await monitor.refresh(force=True)
    assert monitor.samples[INDEX][-1]==dict(time='2026-10-14T09:35:00+08:00',price=12.5)
    assert len(monitor.samples[INDEX])==len(before)+1


async def test_arbitrary_ashare_watchlist_is_persistent_without_account_changes(arena,monkeypatch):
    monitor=arena.market_monitor
    before=copy.deepcopy(arena.state)
    template=copy.deepcopy((await arena.market.quotes())[INDEX])
    async def quotes(symbols):return {s:dict(template,symbol=s,name='测试股票') for s in symbols}
    monkeypatch.setattr(arena.market,'quotes',quotes)
    await monitor.add('600001')
    assert 'sh600001' in monitor.public()['watchlist']
    restored=MarketMonitor(arena.market,arena.db,arena.configuration.settings)
    assert 'sh600001' in restored.watchlist
    await monitor.remove('sh600001')
    assert 'sh600001' not in monitor.public()['watchlist']
    assert arena.state==before
    with pytest.raises(ValueError):await monitor.add('not-a-stock')


async def test_daily_chart_cache_and_provider_failure_are_explicit(arena,monkeypatch):
    monitor=arena.market_monitor
    await monitor.refresh(force=True)
    first=await monitor.detail(INDEX)
    assert first['bars'] and first['samples']
    monitor.bars_cache[INDEX]['at']=-float('inf')
    async def fail(symbol):raise RuntimeError('no bars')
    monkeypatch.setattr(arena.market,'bars',fail)
    second=await monitor.detail(INDEX)
    assert second['bars']==first['bars'] and second['error']


async def test_market_session_uses_quote_freshness_not_an_animated_clock(arena,monkeypatch):
    import app.market_monitor as module
    monitor=arena.market_monitor
    monitor.quotes[INDEX]={'time':'2026-10-14T09:34:50+08:00'}
    monkeypatch.setattr(module,'now',lambda:datetime(2026,10,14,9,35,tzinfo=CST))
    assert monitor.public()['session']=='trading'
    monitor.quotes[INDEX]['time']='2026-10-13T15:00:00+08:00'
    assert monitor.public()['session']=='waiting'
    monkeypatch.setattr(module,'now',lambda:datetime(2026,10,14,0,35,tzinfo=CST))
    assert monitor.public()['session']=='closed'


async def test_live_market_api_preserves_replay_and_has_no_model_calls(arena,dataset,monkeypatch):
    monkeypatch.setattr(app.state,'arena',arena,raising=False)
    await arena.new('replay',dataset['dates'][10],end=dataset['dates'][12])
    before=copy.deepcopy(arena.state)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        response=await client.get('/api/market/live?refresh=true')
        assert response.status_code==200 and response.json()['quotes']
        detail=await client.get('/api/market/stocks/'+INDEX)
        assert detail.status_code==200 and detail.json()['bars']
        assert (await client.get('/api/market/stocks/bad-code')).status_code==400
    assert arena.state==before and arena.agents.calls==0
