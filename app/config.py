import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1]
CST = timezone(timedelta(hours=8))
def now(): return datetime.now(CST)
def timestamp(): return now().isoformat(timespec='seconds')
def load_env():
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k,v = line.split('=',1)
                os.environ.setdefault(k.strip(),v.strip().strip('\"\''))

# Initial observation list only. Agents may discover and trade any supported A-share symbol.
UNIVERSE = {
 'sh600519': {'name':'贵州茅台','sector':'白酒消费'},
 'sz000333': {'name':'美的集团','sector':'家电制造'},
 'sh600036': {'name':'招商银行','sector':'银行'},
 'sh601318': {'name':'中国平安','sector':'保险金融'},
 'sh600900': {'name':'长江电力','sector':'公用事业'},
 'sh601899': {'name':'紫金矿业','sector':'有色金属'},
 'sz002594': {'name':'比亚迪','sector':'新能源汽车'},
 'sh600030': {'name':'中信证券','sector':'证券'},
}
INDEX = 'sh000300'

def normalize_symbol(value):
    import re
    value=str(value).strip().lower()
    if re.fullmatch(r'\d{6}\.(sh|sz|bj)',value): value=value[-2:]+value[:6]
    if re.fullmatch(r'\d{6}',value):
        value=('sh' if value.startswith('6') else 'bj' if value.startswith(('4','8','9')) else 'sz')+value
    if not re.fullmatch(r'(sh6\d{5}|sz[03]\d{5}|bj[489]\d{5})',value):
        raise ValueError('请输入有效沪深北 A 股代码，例如 sh600519、sz300750、bj920001')
    return value

def board_rules(symbol):
    if symbol.startswith('sh68'): return dict(board='科创板',minimum=200,increment=1,maximum=100000,limit=.20)
    if symbol.startswith('sz30'): return dict(board='创业板',minimum=100,increment=100,maximum=1000000,limit=.20)
    if symbol.startswith('bj'): return dict(board='北交所',minimum=100,increment=1,maximum=1000000,limit=.30)
    return dict(board='主板',minimum=100,increment=100,maximum=1000000,limit=.10)
PLAYERS = [
 {'id':'value','name':'deepseek-1','title':'价值研究员','color':'#80e8cd','initial':'1',
  'style':'关注公司竞争优势、主营业务和估值证据。主动研究后选择少数有把握的股票，耐心持有。不能把缺少证据的印象当事实。'},
 {'id':'momentum','name':'deepseek-2','title':'趋势猎手','color':'#b0a0ff','initial':'2',
  'style':'关注量价趋势、相对强弱、板块轮动。自己比较多个标的走势，主动捕捉机会，同时给出风险和退出条件。'},
 {'id':'defender','name':'deepseek-3','title':'风险管家','color':'#ffbc80','initial':'3',
  'style':'优先控制回撤、保持现金和分散风险，研究稳健现金流与行业风险。环境不利时可以主动减仓或持币，不必为交易而交易。'},
 {'id':'contrarian','name':'deepseek-4','title':'逆向观察者','color':'#ec91b8','initial':'4',
  'style':'寻找情绪过度反应后的价格修复。比较下跌幅度、成交量和基本面证据，区分暂时波动与长期恶化；分批试错并明确止损。'},
 {'id':'rotation','name':'deepseek-5','title':'轮动探索者','color':'#74b9f5','initial':'5',
  'style':'比较不同行业标的的相对强弱与量价变化，自主发现行业轮动线索。避免把行业标签当作买入依据，验证证据后分散配置。'},
 {'id':'adaptive','name':'deepseek-6','title':'策略实验员','color':'#d9d27d','initial':'6',
  'style':'把每轮交易当作可证伪的假设。先记录触发条件和退出条件，再从真实成交和未成交反馈中修订规则，优先检验简单且可复现的策略。'},
]
MODELS = ['deepseek-flash','deepseek-v4-pro']
MAX_PLAYERS = 8

LEGACY_AGENT_CODES = dict(zip(('远见','逐浪','守衡','逆流','星图','知新'),
                             (f'deepseek-{i}' for i in range(1,7))))

def use_agent_code(profile):
    """Upgrade only legacy preset names; preserve custom names and stable IDs."""
    code = LEGACY_AGENT_CODES.get(profile.get('name'))
    if code:
        profile['name'] = code
        if 'initial' in profile: profile['initial'] = code.rsplit('-',1)[1]
    return bool(code)
