"""Integer-cent paper ledger, limit orders, reservations and T+1 lots."""
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from .config import timestamp, normalize_symbol, board_rules
from .market import market_open

INITIAL = 100_000_000  # 1,000,000 CNY in cents.
def cents(value): return int((Decimal(str(value))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
def rate(amount,r): return int((Decimal(amount)*Decimal(r)).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
def fees(amount,side,settings=None):
    settings=settings or {}
    return max(cents(settings.get('minimum_commission_yuan',5)),rate(amount,str(settings.get('commission_bps',3)/10000))) + rate(amount,'0.00001') + (rate(amount,'0.0005') if side=='sell' else 0)
def quantity(player,symbol): return sum(l['qty'] for l in player['positions'].get(symbol,[]))
def available(player,symbol,day): return sum(l['qty'] for l in player['positions'].get(symbol,[]) if l['date']<day)
def reserved(player): return sum(o['reserve'] for o in player['orders'] if o['status']=='pending' and o['side']=='buy')
def equity(player,quotes):
    return player['cash']+sum(sum(l['qty'] for l in lots)*cents(quotes[s]['price']) for s,lots in player['positions'].items() if s in quotes)


def submit(player,symbol,side,qty,limit,reason,quotes,day,round_no,source='llm',settings=None):
    settings=settings or {}
    symbol=normalize_symbol(symbol)
    if side not in ('buy','sell'): raise ValueError('只支持买入或卖出')
    rules=board_rules(symbol)
    odd_lot_sale=side=='sell' and qty==available(player,symbol,day) and qty>0
    if type(qty) is not int or qty<=0 or qty>rules['maximum'] or (not odd_lot_sale and (qty<rules['minimum'] or qty%rules['increment'])):
        raise ValueError(f"{rules['board']}申报最少{rules['minimum']}股，递增{rules['increment']}股；不足起点的余股只能一次卖出")
    price=cents(limit)
    if price<=0: raise ValueError('限价必须大于零')
    q=quotes.get(symbol)
    if not q: raise ValueError('没有有效行情，拒绝提交')
    if not .6*q['price'] <= limit <= 1.4*q['price']: raise ValueError('限价偏离参考报价超过 40%')
    pending=[o for o in player['orders'] if o['status']=='pending']
    if len(pending)>=8: raise ValueError('最多保留 8 笔未完成委托')
    amount=price*qty
    reserve=amount+fees(amount,side,settings) if side=='buy' else 0
    if side=='buy':
        if reserve>player['cash']-reserved(player): raise ValueError('可用资金不足（已扣除其他委托冻结额）')
        future=quantity(player,symbol)+qty+sum(o['qty'] for o in pending if o['symbol']==symbol and o['side']=='buy')
        maximum=settings.get('max_position_pct',45)
        if future*max(price,cents(q['price']))>equity(player,quotes)*maximum/100: raise ValueError(f'超过比赛单票 {maximum}% 仓位上限')
    else:
        locked=sum(o['qty'] for o in pending if o['symbol']==symbol and o['side']=='sell')
        if qty>available(player,symbol,day)-locked: raise ValueError('可卖股数不足：当天买入的股票受 T+1 限制')
    order=dict(id=f'{player["id"]}-{len(player["orders"])+1}',symbol=symbol,name=q['name'],side=side,
        qty=qty,limit=price,reason=str(reason)[:500],status='pending',reserve=reserve,created_day=day,
        created_at=timestamp(),created_round=round_no,source=source,message='等待后续有效行情撮合')
    player['orders'].append(order)
    return order


def cancel(player,order_id):
    for order in player['orders']:
        if order['id']==order_id and order['status']=='pending':
            order['status']='cancelled'; order['message']='Agent 主动撤单'; return order
    raise ValueError('没有这笔可撤销的委托')


def match_orders(player,quotes,day,round_no,mode,bars=None,current=None,settings=None):
    settings=settings or {}
    fills=[]
    for order in player['orders']:
        if order['status']!='pending' or order['created_round']>=round_no: continue
        symbol=order['symbol']; q=quotes.get(symbol)
        if not q: order['message']='缺少行情，未撮合'; continue
        if mode=='live':
            if not market_open(q,current): order['message']='休市或报价过期，等待交易时段的新报价'; continue
            if datetime.fromisoformat(q['time'])<=datetime.fromisoformat(order['created_at']): continue
            raw=cents(q['price'])
        else:
            if day<=order['created_day']: continue
            b=(bars or {}).get(symbol)
            if not b or b['date']!=day: order['message']='停牌或缺少当日日线'; continue
            raw=cents(b['open'])
        if q['volume']<=0: order['message']='无成交量，未模拟成交'; continue
        # Conservative approximation: no buying at upper limit, no selling at lower limit.
        if order['side']=='buy' and q.get('upper',0)>0 and raw>=cents(q['upper']):
            order['message']='涨停方向不假设可买入'; continue
        if order['side']=='sell' and q.get('lower',0)>0 and raw<=cents(q['lower']):
            order['message']='跌停方向不假设可卖出'; continue
        slip=rate(raw,str(settings.get('slippage_bps',5)/10000))
        px=raw+slip if order['side']=='buy' else raw-slip
        if (order['side']=='buy' and px>order['limit']) or (order['side']=='sell' and px<order['limit']):
            order['message']='参考价格加滑点未满足限价'
            if mode=='replay': order['status']='expired'; order['message']+='，单日委托过期'
            continue
        amount=px*order['qty']; cost=fees(amount,order['side'],settings)
        if order['side']=='buy':
            other_reserved=reserved(player)-order['reserve']
            if amount+cost>player['cash']-other_reserved:
                order['status']='rejected'; order['message']='撮合时可用资金不足'; continue
            player['cash']-=amount+cost
            player['positions'].setdefault(symbol,[]).append(dict(qty=order['qty'],date=day,price=px,cost=amount+cost))
        else:
            if available(player,symbol,day)<order['qty']:
                order['status']='rejected'; order['message']='撮合时可卖数量不足'; continue
            remaining=order['qty']; basis=0
            for lot in player['positions'][symbol]:
                take=min(remaining,lot['qty']) if lot['date']<day else 0
                if take:
                    portion=lot['cost'] if take==lot['qty'] else lot['cost']*take//lot['qty']
                    lot['cost']-=portion; basis+=portion; lot['qty']-=take; remaining-=take
            player['positions'][symbol]=[l for l in player['positions'][symbol] if l['qty']]
            if not player['positions'][symbol]: del player['positions'][symbol]
            player['cash']+=amount-cost
            player['realized']+=amount-cost-basis
        order.update(status='filled',fill_price=px,fee=cost,fill_day=day,fill_round=round_no,message='模拟成交；不代表真实盘口可成交')
        fills.append(order)
    return fills
