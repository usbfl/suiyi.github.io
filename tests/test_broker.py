import copy
from datetime import datetime
import pytest
from app.broker import submit,cancel,match_orders,INITIAL,equity,available,reserved,fees,cents
from app.config import board_rules,normalize_symbol,CST
from app.market import market_open

def player():return dict(id='p',cash=INITIAL,positions={},orders=[],realized=0)
def quote(symbol='sh600519',price=10):
    return {symbol:dict(name='测试股票',symbol=symbol,price=price,previous=10,volume=10000,
        upper=11,lower=9,date='2026-10-13',time='2026-10-13T10:00:00+08:00')}

def buy(p):
    q=quote();submit(p,'sh600519','buy',100,10.1,'test',q,'2026-10-12',1)
    bars={'sh600519':dict(date='2026-10-13',open=10)}
    return match_orders(p,q,'2026-10-13',2,'replay',bars)

def test_cash_reservation_and_cancel():
    p=player();q=quote();o=submit(p,'sh600519','buy',100,10,'test',q,'2026-10-12',1)
    assert reserved(p)==100000+fees(100000,'buy') and p['cash']==INITIAL
    cancel(p,o['id']);assert reserved(p)==0

def test_fill_and_t_plus_one():
    p=player();fills=buy(p);o=fills[0]
    assert p['cash']==INITIAL-o['fill_price']*100-o['fee']
    assert available(p,'sh600519','2026-10-13')==0
    with pytest.raises(ValueError,match=r'T\+1'):submit(p,'sh600519','sell',100,10,'sell',quote(),'2026-10-13',3)
    submit(p,'sh600519','sell',100,9.9,'sell',quote(),'2026-10-14',3)
    match_orders(p,quote(),'2026-10-15',4,'replay',{'sh600519':dict(date='2026-10-15',open=10)})
    assert not p['positions'] and p['cash']<INITIAL and p['realized']==p['cash']-INITIAL

def test_cannot_spend_or_sell_reserved_twice():
    p=player();p['cash']=150000
    # Increase account value with an unrelated holding so risk limit does not mask cash check.
    p['positions']['sh600036']=[dict(qty=1000,date='2026-10-10',cost=1000000,price=1000)]
    q={**quote(),**quote('sh600036')}
    submit(p,'sh600519','buy',100,10,'test',q,'2026-10-12',1)
    with pytest.raises(ValueError,match='资金'):submit(p,'sh600519','buy',100,10,'test',q,'2026-10-12',1)
    submit(p,'sh600036','sell',800,10,'test',q,'2026-10-12',1)
    with pytest.raises(ValueError,match='可卖'):submit(p,'sh600036','sell',800,10,'test',q,'2026-10-12',1)

@pytest.mark.parametrize('symbol,qty,ok',[('sh600519',101,False),('sh688981',201,True),('sh688981',100,False),('bj920001',101,True),('sz300750',101,False),('sz300750',100,True)])
def test_board_lots_and_unrestricted_symbols(symbol,qty,ok):
    if ok:assert submit(player(),symbol,'buy',qty,10,'test',quote(symbol),'2026-10-12',1)['symbol']==symbol
    else:
        with pytest.raises(ValueError):submit(player(),symbol,'buy',qty,10,'test',quote(symbol),'2026-10-12',1)

def test_old_quote_and_market_closed_cannot_fill():
    p=player();q=quote();o=submit(p,'sh600519','buy',100,10.1,'test',q,'2026-10-12',1)
    o['created_at']='2026-10-12T16:00:00+08:00'
    match_orders(p,q,'2026-10-13',2,'live',current=datetime(2026,10,13,10,5,tzinfo=CST))
    assert o['status']=='pending' and p['cash']==INITIAL
    match_orders(p,q,'2026-10-13',2,'live',current=datetime(2026,10,13,10,0,30,tzinfo=CST))
    assert o['status']=='filled'
    assert not market_open(q['sh600519'],datetime(2026,10,13,12,0,tzinfo=CST))

def test_no_same_round_fill_or_upper_limit_fill():
    p=player();q=quote(price=11);submit(p,'sh600519','buy',100,11.1,'test',q,'2026-10-12',1)
    bars={'sh600519':dict(date='2026-10-13',open=11)}
    assert not match_orders(p,q,'2026-10-13',1,'replay',bars)
    assert not match_orders(p,q,'2026-10-13',2,'replay',bars)

def test_risk_limit_and_normalization():
    with pytest.raises(ValueError,match='45%'):submit(player(),'sh600519','buy',50000,10,'test',quote(),'2026-10-12',1)
    assert normalize_symbol('300750')=='sz300750'
    assert normalize_symbol('688981.SH')=='sh688981'
    with pytest.raises(ValueError):normalize_symbol('sh000001')


def test_configured_position_limit_changes_real_order_acceptance():
    p = player()
    with pytest.raises(ValueError, match='10%'):
        submit(p, 'sh600519', 'buy', 20000, 10, 'test', quote(), '2026-10-12', 1,
            settings={'max_position_pct': 10})
    assert p['orders'] == [] and p['cash'] == INITIAL
    order = submit(p, 'sh600519', 'buy', 20000, 10, 'test', quote(), '2026-10-12', 1,
        settings={'max_position_pct': 30})
    assert order['status'] == 'pending'


def test_configured_slippage_and_commission_change_ledger_fill():
    p = player()
    settings = dict(max_position_pct=45, slippage_bps=100, commission_bps=10,
        minimum_commission_yuan=2)
    submit(p, 'sh600519', 'buy', 100, 10.1, 'test', quote(), '2026-10-12', 1, settings=settings)
    fills = match_orders(p, quote(), '2026-10-13', 2, 'replay',
        {'sh600519': dict(date='2026-10-13', open=10)}, settings=settings)
    assert len(fills) == 1
    assert fills[0]['fill_price'] == 1010  # 10 yuan + 100 bp = 10.10 yuan.
    assert fills[0]['fee'] == 201  # 2 yuan minimum commission + 0.01 yuan transfer fee.
    assert p['cash'] == INITIAL - 101000 - 201
