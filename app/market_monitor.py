"""Read-only live quotes, independent of experiment dates and model execution."""
import asyncio
import copy
import json
import time
import uuid
from .config import INDEX, UNIVERSE, normalize_symbol, now, timestamp
from .market import SOURCE, market_open


class MarketMonitor:
    def __init__(self, market, db, settings):
        self.market, self.db, self.settings = market, db, settings
        row = db.execute("SELECT value FROM settings WHERE key='market_watchlist'").fetchone()
        self.watchlist = json.loads(row[0]) if row else list(UNIVERSE)
        self.quotes = {}
        self.samples = {}
        self.bars_cache = {}
        self.checked_at = None
        self.error = ''
        self.unavailable = []
        self.revision = 0
        self.monitor_id = uuid.uuid4().hex[:12]
        self.last_attempt = -float('inf')
        self.lock = asyncio.Lock()
        self.bars_lock = asyncio.Lock()
        self.task = None

    @property
    def interval(self):
        return self.settings()['quote_refresh_seconds']

    def _record(self, quotes):
        for symbol, quote in quotes.items():
            self.quotes[symbol] = quote
            points = self.samples.setdefault(symbol, [])
            point = dict(time=quote['time'], price=quote['price'])
            if not points or points[-1] != point:
                points.append(point)
                del points[:-240]

    async def refresh(self, force=False):
        async with self.lock:
            if not force and time.monotonic() - self.last_attempt < self.interval:
                return self.public()
            symbols = [INDEX, *self.watchlist]
            self.last_attempt = time.monotonic()
            try:
                fresh = await self.market.quotes(symbols)
                fresh = {s:q for s,q in fresh.items() if s in symbols}
                if not fresh: raise ValueError('没有有效报价')
                self._record(fresh)
                self.unavailable = [s for s in symbols if s not in fresh]
                self.error = '部分股票未返回最新报价，保留其原始报价时间。' if self.unavailable else ''
            except Exception:
                self.error = '行情源暂时不可用，保留最近有效报价，自动重试。'
                self.unavailable = symbols
            self.checked_at = timestamp()
            self.revision += 1
            return self.public()

    def public(self):
        current = now()
        quote = self.quotes.get(INDEX, {})
        clock = current.strftime('%H:%M:%S')
        if market_open(quote, current):
            session, label = 'trading', '连续交易中'
        elif current.weekday() >= 5 or clock < '09:15:00' or clock >= '15:00:00':
            session, label = 'closed', '非交易时段 · 最近有效报价'
        elif '11:30:00' <= clock < '13:00:00':
            session, label = 'break', '午间休市 · 自动检查继续'
        else:
            session, label = 'waiting', '等待有效行情 · 请核对报价时间'
        return dict(monitor_id=self.monitor_id,revision=self.revision, source=SOURCE, checked_at=self.checked_at,
            server_time=current.isoformat(timespec='seconds'), refresh_seconds=self.interval,
            session=session, session_label=label, error=self.error,
            unavailable_symbols=self.unavailable, watchlist=list(self.watchlist),
            quotes=[copy.deepcopy(self.quotes[s]) for s in [INDEX,*self.watchlist] if s in self.quotes])

    async def detail(self, raw):
        symbol = INDEX if raw == INDEX else normalize_symbol(raw)
        await self.refresh()
        quote = self.quotes.get(symbol)
        if quote is None:
            try: result = await self.market.quotes([symbol])
            except Exception: raise ValueError('该股票行情暂时不可用，请稍后重试') from None
            quote = result.get(symbol)
            if not quote: raise ValueError('未取得该股票的有效报价')
        error = ''
        async with self.bars_lock:
            cached = self.bars_cache.get(symbol)
            if not cached or time.monotonic() - cached['at'] >= 300:
                try:
                    bars = await self.market.bars(symbol)
                    cached = dict(at=time.monotonic(), bars=bars)
                    self.bars_cache[symbol] = cached
                except Exception:
                    error = '日线暂时不可用。' if not cached else '日线刷新失败，展示最近取得的数据。'
            bars = copy.deepcopy(cached['bars']) if cached else []
        return dict(symbol=symbol, quote=copy.deepcopy(quote), bars=bars,
            samples=copy.deepcopy(self.samples.get(symbol,[])), error=error, source=SOURCE)

    async def add(self, raw):
        symbol = normalize_symbol(raw)
        async with self.lock:
            if symbol in self.watchlist: return self.public()
            if len(self.watchlist) >= 60: raise ValueError('自选列表最多 60 只股票，可先移除一个标的')
            try: result = await self.market.quotes([symbol])
            except Exception: raise ValueError('该股票行情暂时不可用，请稍后重试') from None
            if symbol not in result: raise ValueError('未取得该股票的有效报价')
            self.watchlist.append(symbol)
            self._record({symbol:result[symbol]})
            self._save_watchlist()
            self.revision += 1
            return self.public()

    async def remove(self, raw):
        symbol = normalize_symbol(raw)
        async with self.lock:
            self.watchlist = [s for s in self.watchlist if s != symbol]
            self._save_watchlist()
            self.revision += 1
            return self.public()

    def _save_watchlist(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('market_watchlist',?)",
                (json.dumps(self.watchlist),))

    async def search(self, query):
        query = str(query).strip()[:80]
        if not query: return []
        try: symbol = normalize_symbol(query)
        except ValueError:
            try: return await self.market.search(query)
            except Exception: raise ValueError('股票检索服务暂时不可用，请稍后重试') from None
        try: quotes = await self.market.quotes([symbol])
        except Exception: raise ValueError('股票检索服务暂时不可用，请稍后重试') from None
        return [dict(symbol=symbol,name=quotes[symbol]['name'])] if symbol in quotes else []

    def start(self):
        async def loop():
            while True:
                await self.refresh()
                await asyncio.sleep(self.interval)
        self.task = asyncio.create_task(loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
