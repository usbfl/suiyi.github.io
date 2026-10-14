"""Public data adapters. Never fabricate prices or silently replace raw bars with adjusted bars."""
import asyncio
import json
import re
from datetime import datetime
import httpx
from .config import UNIVERSE, INDEX, ROOT, timestamp, now, CST, normalize_symbol, board_rules

SOURCE = '腾讯财经公开行情（非交易所授权逐笔行情）'


class Market:
    def __init__(self, cache=None):
        self.cache = cache or ROOT / 'data/market.json'
        self.client = httpx.AsyncClient(timeout=15, headers={'User-Agent':'Mozilla/5.0'}, follow_redirects=False)
        self.sem = asyncio.Semaphore(3)
        self.lock = asyncio.Lock()

    async def bars(self, symbol):
        async with self.sem:
            r=await self.client.get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',params={'param':f'{symbol},day,,,120,'})
            r.raise_for_status()
            payload=r.json()
            data=payload.get('data',{}).get(symbol,{}).get('day')
            if not data: raise ValueError(f'{symbol} 日线数据不可用')
            output=[]
            for row in data:
                bar=dict(date=row[0],open=float(row[1]),close=float(row[2]),high=float(row[3]),low=float(row[4]),volume=float(row[5]))
                if bar['date'] > now().date().isoformat(): continue
                if min(bar[k] for k in ('open','close','high','low')) <= 0: continue
                output.append(bar)
            if not output: raise ValueError('没有有效历史行情')
            return output

    async def quotes(self, symbols=None):
        symbols=symbols or list(UNIVERSE)+[INDEX]
        r=await self.client.get('https://qt.gtimg.cn/q='+','.join(symbols))
        r.raise_for_status()
        text=r.content.decode('gb18030',errors='replace')
        output={}
        for symbol,body in re.findall(r'v_(\w+)="([^"]*)"',text):
            f=body.split('~')
            if len(f)<35 or symbol not in symbols: continue
            try:
                qt=datetime.strptime(f[30],'%Y%m%d%H%M%S').replace(tzinfo=CST)
                price=float(f[3]); previous=float(f[4])
                if price<=0 or previous<=0: continue
                output[symbol]=dict(symbol=symbol,price=price,previous=previous,open=float(f[5]),
                    high=float(f[33]),low=float(f[34]),volume=float(f[6]),change=float(f[32]),
                    time=qt.isoformat(),date=qt.date().isoformat(),
                    upper=float(f[47]) if len(f)>48 and symbol!=INDEX else 0,
                    lower=float(f[48]) if len(f)>48 and symbol!=INDEX else 0,
                    name=f[1],rules=board_rules(symbol) if symbol!=INDEX else {})
            except (ValueError,IndexError): continue
        if not output: raise ValueError('行情接口没有返回有效报价')
        return output

    async def search(self,query):
        r=await self.client.get('https://smartbox.gtimg.cn/s3/',params={'q':str(query)[:80],'t':'all'})
        r.raise_for_status()
        found=re.search(r'v_hint="(.*?)"',r.text,re.S)
        if not found: return []
        decoded=json.loads('"'+found.group(1)+'"')
        results=[]
        for row in decoded.split('^'):
            f=row.split('~')
            if len(f)<5 or f[4]!='GP-A': continue
            try: symbol=normalize_symbol(f[0]+f[1])
            except ValueError: continue
            results.append(dict(symbol=symbol,name=f[2],rules=board_rules(symbol)))
        return results[:20]

    async def scan(self,sort='active'):
        r=await self.client.get('https://82.push2.eastmoney.com/api/qt/clist/get',params={
            'pn':1,'pz':30,'po':0 if sort=='losers' else 1,'np':1,'fltt':2,'invt':2,
            'fid':'f6' if sort=='active' else 'f3',
            'fs':'m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048',
            'fields':'f12,f14,f2,f3,f13,f6'})
        r.raise_for_status(); body=r.json().get('data') or {}; results=[]
        for row in body.get('diff',[]):
            try: symbol=normalize_symbol(str(row['f12']))
            except ValueError: continue
            results.append(dict(symbol=symbol,name=row['f14'],price=row['f2'],change=row['f3'],amount=row['f6'],rules=board_rules(symbol)))
        return dict(total=body.get('total'),stocks=results,fetched_at=timestamp(),note='当前全市场榜单；不是历史时点榜单')

    async def dataset(self, refresh=False):
        async with self.lock:
            cached=None
            if self.cache.exists():
                cached=json.loads(self.cache.read_text(encoding='utf8'))
                fetched=cached.get('fetched_at','')
                if not refresh and fetched[:10]==now().date().isoformat():
                    # A cache captured before close must also be refreshed after close.
                    if now().hour<16 or (len(fetched)>12 and int(fetched[11:13])>=16):
                        return cached
            try:
                symbols=list(UNIVERSE)+[INDEX]
                bars=await asyncio.gather(*(self.bars(s) for s in symbols))
                data=dict(source=SOURCE,fetched_at=timestamp(),adjustment='不复权',bars=dict(zip(symbols,bars)))
                # Drop today's still-open daily candle, which would leak incomplete future data in replay.
                if now().hour < 16:
                    for s in symbols:
                        data['bars'][s]=[b for b in data['bars'][s] if b['date']<now().date().isoformat()]
                common=set.intersection(*({b['date'] for b in series} for series in data['bars'].values()))
                data['dates']=sorted(common)
                if len(common)<35: raise ValueError('共同交易日不足')
                self.cache.parent.mkdir(parents=True,exist_ok=True)
                self.cache.write_text(json.dumps(data,ensure_ascii=False),encoding='utf8')
                return data
            except Exception:
                if cached:
                    return dict(cached,warning='数据刷新失败，正在使用带原始时间戳的本地缓存')
                raise ValueError('无法获取真实行情，请稍后重试；本系统不会生成虚假价格。')


def historical_quotes(dataset, day):
    result={}
    for symbol,series in dataset['bars'].items():
        visible=[b for b in series if b['date']<=day]
        if not visible: continue
        b=visible[-1]; prev=visible[-2]['close'] if len(visible)>1 else b['open']
        result[symbol]=dict(symbol=symbol,name=dataset.get('names',{}).get(symbol,UNIVERSE.get(symbol,{'name':symbol})['name']),
            price=b['close'],previous=prev,open=b['open'],high=b['high'],low=b['low'],volume=b['volume'],
            change=round((b['close']/prev-1)*100,2),time=b['date']+'T15:00:00+08:00',date=b['date'],
            upper=round(prev*(1+board_rules(symbol)['limit']),2),lower=round(prev*(1-board_rules(symbol)['limit']),2),
            rules=board_rules(symbol),limit_estimated=True)
    return result


def market_open(quote, current=None):
    current=current or now()
    try: observed=datetime.fromisoformat(quote['time'])
    except (KeyError,ValueError): return False
    clock=current.strftime('%H:%M:%S')
    return (current.weekday()<5 and ('09:30:00'<=clock<'11:30:00' or '13:00:00'<=clock<'14:57:00')
            and observed.date()==current.date() and 0 <= (current-observed).total_seconds() <= 120)
