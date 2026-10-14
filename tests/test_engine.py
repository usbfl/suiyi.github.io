import asyncio
import copy
import json
import pytest
from app.engine import Arena
from app.config import INDEX
from app.research import allowed_url

async def policy(player,context,execute,emit):
    if not context['account']['holdings']:
        result=await execute('place_order',dict(symbol='sh600519',side='buy',quantity=100,limit_price=12,reason='测试委托'))
        await emit('place_order','测试委托',{},result)
    await execute('finish',dict(summary='test',risk='test',lesson='从实际反馈总结',next_strategy='根据上轮反馈调整仓位，版本'+str(context['round'])))
    return 'test'

async def test_replay_feedback_future_guard_and_idempotency(arena,monkeypatch,dataset):
    monkeypatch.setattr(arena.agents,'run',policy)
    await arena.new('replay',dataset['dates'][30])
    await asyncio.gather(arena.step('same-intent'),arena.step('same-intent'))
    assert arena.state['round']==1
    first_strategy=arena.state['players'][0]['strategy']
    await arena.step('second-intent')
    p=arena.state['players'][0]
    assert len(p['versions'])==2 and p['strategy']!=first_strategy
    assert p['positions'] and p['cash']<100000000
    result=await arena.execute(arena.state,p,{},'price_history',{'symbol':'sh600519'})
    assert all(b[0]<=arena.state['day'] for b in result['bars'])
    for tool,args in [('search_web',{'query':'future'}),('company_profile',{'symbol':'sh600519'}),('read_source',{'source_id':'x'})]:
        with pytest.raises(ValueError,match='历史回溯'):await arena.execute(arena.state,p,{},tool,args)

async def test_commit_failure_rolls_back_portfolios(arena,monkeypatch,dataset):
    monkeypatch.setattr(arena.agents,'run',policy)
    await arena.new('replay',dataset['dates'][30]);before=copy.deepcopy(arena.state)
    arena.db.executescript("CREATE TRIGGER fail_snapshot BEFORE INSERT ON snapshots BEGIN SELECT RAISE(ABORT,'disk failure'); END;")
    with pytest.raises(Exception,match='disk failure'):await arena.step('failed-round')
    assert arena.state==before
    assert not arena.db.execute('SELECT 1 FROM commands WHERE request_id=?',('failed-round',)).fetchone()
    assert all(r[0]=='aborted' for r in arena.db.execute('SELECT status FROM traces'))

async def test_recovery_keeps_accounts_and_strategy(arena,monkeypatch,dataset):
    monkeypatch.setattr(arena.agents,'run',policy)
    await arena.new('replay',dataset['dates'][30]);await arena.step('recover-test')
    path=arena.db.execute('PRAGMA database_list').fetchone()[2]
    second=Arena(path)
    try:
        await second.initialize()
        assert second.state==arena.state
        assert not second.running
    finally:await second.close()

def test_source_url_guard():
    assert allowed_url('https://static.cninfo.com.cn/test.pdf')
    for url in ['http://localhost/a','https://127.0.0.1/a','https://cninfo.com.cn.attacker.test/a','file:///etc/passwd','https://user:pass@cninfo.com.cn/a']:
        assert not allowed_url(url)

async def test_model_money_units_are_yuan_not_internal_cents(arena):
    p=arena.state['players'][0]
    assert p['cash']==100000000
    account=arena.agent_account(p,arena.state)
    assert account['cash']==1000000 and account['equity']==1000000
    assert 'CNY' in account['money_unit']


async def test_new_stock_is_discoverable_and_live_history_refreshes(arena,monkeypatch,dataset):
    symbol='sz300308'; calls=[]
    async def bars(s):
        calls.append(s)
        return copy.deepcopy(dataset['bars'][INDEX])
    async def quotes(symbols):
        return {symbol:{**arena.state['quotes'][INDEX],'symbol':symbol,'name':'测试证券'}}
    monkeypatch.setattr(arena.market,'bars',bars)
    monkeypatch.setattr(arena.market,'quotes',quotes)
    assert symbol not in arena.state['watchlist']
    await arena.ensure_asset(arena.state,symbol)
    await arena.ensure_asset(arena.state,symbol)
    assert calls==[symbol] and symbol in arena.state['watchlist']
    arena.state['dataset']['bars_fetched'][symbol]='old-day'
    await arena.ensure_asset(arena.state,symbol)
    assert calls==[symbol,symbol]


async def test_round_failure_cancels_siblings_before_audit_abort(arena,monkeypatch):
    waiting=asyncio.Event(); cancelled=asyncio.Event()
    async def broken(player,context,execute,emit):
        if player['id']==arena.state['players'][0]['id']:
            await waiting.wait()
            raise RuntimeError('unexpected failure')
        await emit('thinking','waiting',{},None)
        waiting.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    monkeypatch.setattr(arena.agents,'run',broken)
    before=copy.deepcopy(arena.state)
    with pytest.raises(RuntimeError,match='unexpected failure'):
        await arena.step('cancel-siblings')
    assert cancelled.is_set() and arena.state==before
    assert all(r[0]=='aborted' for r in arena.db.execute('SELECT status FROM traces'))


@pytest.mark.parametrize('opened,elapsed,move,expected',[
    (False,1000,.02,False),(True,30,.02,False),(True,120,.007,True),
    (True,120,.002,False),(True,901,0,True),
])
async def test_live_wakeup_requires_session_and_trigger(arena,monkeypatch,opened,elapsed,move,expected):
    from datetime import timedelta
    import app.engine as engine
    current=engine.now();calls=[]
    arena.state['last_decision_at']=(current-timedelta(seconds=elapsed)).isoformat()
    arena.state['decision_prices']={s:q['price']/(1+move) for s,q in arena.state['quotes'].items()}
    monkeypatch.setattr(engine,'now',lambda:current)
    monkeypatch.setattr(engine,'market_open',lambda q:opened)
    async def step(ident):calls.append(ident)
    async def sleep(seconds):arena.running=False
    monkeypatch.setattr(arena,'step',step)
    monkeypatch.setattr(engine.asyncio,'sleep',sleep)
    arena.running=True
    await arena.auto()
    assert bool(calls)==expected


async def test_waiting_quote_refresh_cannot_leak_live_data_into_new_replay(arena, monkeypatch):
    calls = []
    async def quotes(symbols=None):
        calls.append(symbols)
        raise AssertionError('A refresh queued during live mode must recheck mode after locking')
    monkeypatch.setattr(arena.market, 'quotes', quotes)
    await arena.lock.acquire()
    task = asyncio.create_task(arena.refresh())
    try:
        await asyncio.sleep(0)  # refresh passed its first mode check and is waiting for the lock.
        replacement = copy.deepcopy(arena.state)
        replacement['mode'] = 'replay'
        arena.state = replacement
        before = copy.deepcopy(replacement)
    finally:
        arena.lock.release()
    await asyncio.wait_for(task, timeout=1)
    assert calls == [] and arena.state == before
