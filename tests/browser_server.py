"""ISOLATED BROWSER FIXTURE ONLY. Never import this module from app/.

Starts the real FastAPI application and static UI on 127.0.0.1:8011, while
replacing external data/model clients with deterministic in-process fixtures.
All accounts live in an OS temporary directory removed when the server stops.
"""
import asyncio
import copy
import os
from datetime import date, timedelta
from pathlib import Path
import sys
import tempfile
from cryptography.fernet import Fernet

# The isolated browser fixture never reuses a real deployment vault key.
os.environ['ALPHALAB_SECRET_KEY'] = Fernet.generate_key().decode()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for key, value in {
    'LLM_API_KEY': 'BROWSER_FIXTURE_ONLY_NO_REAL_PROVIDER', 'LLM_BASE_URL': 'https://network-disabled.invalid',
    'LLM_MAX_CALLS': '1000', 'LLM_TIMEOUT': '5', 'LLM_CONCURRENCY': '3',
}.items():
    os.environ[key] = value

import httpx
import uvicorn
from app.config import INDEX, UNIVERSE
from app.engine import Arena
from app.market import historical_quotes
import app.main as main

TEMP = tempfile.TemporaryDirectory(prefix='alpha-lab-browser-fixture-')
DB = Path(TEMP.name) / 'fixture.sqlite3'
os.environ['ARENA_DB'] = str(DB)


def fixture_data():
    days = []
    day = date(2026, 7, 1)
    while len(days) < 45:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return dict(source='SYNTHETIC BROWSER TEST FIXTURE — NOT MARKET DATA',
        fetched_at='2026-09-01T00:00:00+08:00', dates=days,
        bars={symbol: [dict(date=d, open=10+i*.02, close=10.01+i*.02,
            high=10.2+i*.02, low=9.9+i*.02, volume=100000)
            for i, d in enumerate(days)] for symbol in [*UNIVERSE, INDEX]})


def reject_network(request):
    raise AssertionError('Browser fixture must never make an external network request')


class BrowserArena(Arena):
    def __init__(self, path=None):
        super().__init__(DB)
        self.fixture = fixture_data()
        self.quote_tick = 0
        self.quote_error = False
        self.unused_clients = [self.market.client, self.research.client, self.agents.client]
        for service in (self.market, self.research, self.agents):
            service.client = httpx.AsyncClient(transport=httpx.MockTransport(reject_network))
        self.market.dataset = self.dataset
        self.market.quotes = self.quotes
        self.market.bars = self.bars
        self.agents.run = self.fixture_policy

    async def initialize(self):
        for client in self.unused_clients:
            await client.aclose()
        await super().initialize()

    async def dataset(self, refresh=False):
        return copy.deepcopy(self.fixture)

    async def quotes(self, symbols=None):
        if self.quote_error: raise ValueError('TEST FIXTURE provider outage')
        result = historical_quotes(self.fixture, self.fixture['dates'][-1])
        if symbols:
            result = {s:copy.deepcopy(result.get(s,dict(result[INDEX],symbol=s,name='TEST FIXTURE stock'))) for s in symbols}
        if self.quote_tick:
            for quote in result.values():
                quote.update(price=round(quote['price']+self.quote_tick*.1,2),
                    time=f'2026-10-14T09:35:{self.quote_tick:02d}+08:00',date='2026-10-14')
        return result

    async def bars(self, symbol):
        return copy.deepcopy(self.fixture['bars'].get(symbol, self.fixture['bars'][INDEX]))

    async def fixture_policy(self, player, context, execute, emit):
        await emit('thinking', 'TEST FIXTURE: deterministic tool policy', {}, None)
        await asyncio.sleep(.2)
        self.agents.calls += 1
        self.agents.tokens += 100
        player['calls'] += 1
        player['tokens'] += 100
        result = await execute('price_history', {'symbol': 'sh600519'})
        assert all(bar[0] <= context['date'] for bar in result['bars'])
        await emit('price_history', 'TEST FIXTURE: date-bounded history', {'symbol': 'sh600519'}, result)
        if context['mode'] == 'replay' and context['round'] == 2:
            args = dict(symbol='sh600519', side='buy', quantity=100,
                limit_price=12, reason='TEST FIXTURE: submit before validation boundary')
            result = await execute('place_order', args)
            await emit('place_order', args['reason'], args, result)
        note = 'TEST FIXTURE observation on ' + context['date']
        try:
            result = await execute('save_note', {'note': note})
        except ValueError as exc:
            result = {'error': str(exc)}
        await emit('save_note', note, {'note': note}, result)
        args = dict(summary='TEST FIXTURE review on ' + context['date'], risk='Synthetic data only',
            lesson='TEST FIXTURE feedback', next_strategy='TEST FIXTURE strategy on ' + context['date'])
        result = await execute('finish', args)
        await emit('finish', args['summary'], args, result)
        return result['summary']


main.Arena = BrowserArena


@main.app.get('/__test__/identity')
async def identity():
    return dict(fixture=True, external_network=False, real_models=False,
        temporary_database=True, database_name=DB.name, pid=os.getpid())


@main.app.post('/__test__/shutdown')
async def shutdown():
    server.should_exit = True
    return {'stopping': True}


@main.app.post('/__test__/reset')
async def reset():
    return await main.app.state.arena.new('live', title='ISOLATED BROWSER FIXTURE')


@main.app.post('/__test__/market-tick')
async def market_tick(body:dict):
    arena=main.app.state.arena
    arena.quote_error=bool(body.get('error'))
    if not arena.quote_error: arena.quote_tick+=1
    return await arena.market_monitor.refresh(force=True)


server = uvicorn.Server(uvicorn.Config(main.app, host='127.0.0.1', port=8011, log_level='warning'))
if __name__ == '__main__':
    try:
        server.run()
    finally:
        TEMP.cleanup()
