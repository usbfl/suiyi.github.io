import copy
from datetime import date,timedelta
import pytest
import httpx
from cryptography.fernet import Fernet
from app.config import UNIVERSE,INDEX
from app.engine import Arena
from app.market import historical_quotes


@pytest.fixture(autouse=True)
def safe_env(monkeypatch):
    monkeypatch.setenv('ALPHALAB_SECRET_KEY', Fernet.generate_key().decode())
    # Explicit fake legacy credentials exercise normal routing without loading the user's key.
    for k,v in {'LLM_API_KEY':'SK_TEST_ONLY_NO_PROVIDER_1234567890','LLM_BASE_URL':'https://model.test','LLM_CONCURRENCY':'3','LLM_MAX_CALLS':'100','LLM_TIMEOUT':'10'}.items():
        monkeypatch.setenv(k,v)
    original_init = httpx.AsyncClient.__init__
    def reject_network(request):
        raise AssertionError('Tests must provide an explicit local/mock HTTP transport')
    def isolated_init(self, *args, **kwargs):
        kwargs.setdefault('transport', httpx.MockTransport(reject_network))
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, '__init__', isolated_init)


@pytest.fixture
def dataset():
    # Synthetic data is confined to tests and never imported by the application.
    days=[];d=date(2026,7,1)
    while len(days)<45:
        if d.weekday()<5:days.append(d.isoformat())
        d+=timedelta(days=1)
    return dict(source='TEST FIXTURE ONLY',fetched_at='2026-09-01T00:00:00+08:00',dates=days,
        bars={s:[dict(date=day,open=10+i*.02,close=10.01+i*.02,high=10.2+i*.02,low=9.9+i*.02,volume=100000)
            for i,day in enumerate(days)] for s in list(UNIVERSE)+[INDEX]})


@pytest.fixture
async def arena(tmp_path,dataset,monkeypatch):
    a=Arena(tmp_path/'test.db')
    async def data(refresh=False):return copy.deepcopy(dataset)
    async def quotes(symbols=None):return historical_quotes(dataset,dataset['dates'][-1])
    async def bars(symbol):return copy.deepcopy(dataset['bars'][INDEX])
    monkeypatch.setattr(a.market,'dataset',data);monkeypatch.setattr(a.market,'quotes',quotes);monkeypatch.setattr(a.market,'bars',bars)
    await a.initialize()
    yield a
    await a.close()
