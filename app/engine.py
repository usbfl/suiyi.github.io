import asyncio
import copy
import json
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from .config import ROOT,PLAYERS,MODELS,MAX_PLAYERS,UNIVERSE,INDEX,normalize_symbol,board_rules,timestamp,now,use_agent_code
from .market import Market,historical_quotes,market_open
from .research import Research
from .agent import Agents,TOOLS
from .configuration import Configuration,DEFAULT_SETTINGS
from .market_monitor import MarketMonitor
from .broker import INITIAL,cents,equity,quantity,available,reserved,submit,cancel,match_orders


class Arena:
    def __init__(self,path=None):
        path=Path(path or ROOT/'data/arena.sqlite3'); path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path)
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS matches(id TEXT PRIMARY KEY,created TEXT,mode TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS snapshots(match_id TEXT,revision INTEGER,payload TEXT,PRIMARY KEY(match_id,revision));
            CREATE TABLE IF NOT EXISTS commands(request_id TEXT PRIMARY KEY,match_id TEXT);
            CREATE TABLE IF NOT EXISTS traces(id INTEGER PRIMARY KEY AUTOINCREMENT,match_id TEXT,run_id TEXT,
                player TEXT,round INTEGER,time TEXT,tool TEXT,title TEXT,args TEXT,result TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        ''')
        self.market=Market(); self.research=Research(self.db); self.agents=Agents()
        self.configuration=Configuration(self.db,lambda:[t['function']['name'] for t in TOOLS])
        self.market_monitor=MarketMonitor(self.market,self.db,self.configuration.settings)
        self.agents.configuration=self.configuration
        self.lock=asyncio.Lock(); self.asset_lock=asyncio.Lock()
        self.state=None; self.busy=False; self.running=False; self.task=None; self.ticker=None
        self.phases={}; self.error=''; self.initial_error=''

    async def initialize(self):
        with self.db:self.db.execute("UPDATE traces SET status='interrupted' WHERE status='pending'")
        row=self.db.execute("SELECT payload FROM matches WHERE id=(SELECT value FROM settings WHERE key='active')").fetchone()
        if row: self.state=self.migrate(json.loads(row[0]))
        else:
            try: await self.new('live')
            except Exception as e: self.initial_error=str(e)
        # Quotes update without paying for LLM calls. Orders settle at fresh ticks independently.
        self.ticker=asyncio.create_task(self.price_loop())
        self.market_monitor.start()

    def commit(self,state,request_id=None,run_id=None,checkpoint=True):
        payload=json.dumps(state,ensure_ascii=False)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO matches VALUES(?,?,?,?)',(state['id'],state['created'],state['mode'],payload))
            if checkpoint:self.db.execute('INSERT INTO snapshots VALUES(?,?,?)',(state['id'],state['revision'],payload))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('active',?)",(state['id'],))
            if request_id:self.db.execute('INSERT INTO commands VALUES(?,?)',(request_id,state['id']))
            if run_id:self.db.execute("UPDATE traces SET status='committed' WHERE run_id=?",(run_id,))
        self.state=state

    def migrate(self,state):
        """Fill missing metadata and normalize legacy labels without rewriting snapshots."""
        for player in state['players']: use_agent_code(player)
        state.setdefault('title',('历史实验' if state['mode']=='replay' else '实时竞技场')+' · '+state['created'][:10])
        state.setdefault('runtime_settings',copy.deepcopy(DEFAULT_SETTINGS))
        if state['mode']=='replay' and 'replay_plan' not in state:
            first=state['players'][0]['history'][0]['date']
            dates=[d for d in state['dataset']['dates'] if d>=first]
            state['replay_plan']=dict(start=first,end=dates[-1],validation_start=None,dates=dates,total_rounds=len(dates),legacy=True)
        return state

    def profiles(self,configs=None,models=None):
        if configs is not None and models is not None:raise ValueError('players 和 models 只能提供一个')
        if configs is None:
            if models is None:configs=self.configuration.profiles()[:6]
            else:configs=[dict(preset=PLAYERS[i%len(PLAYERS)]['id'],model=model,name=f'deepseek-{i+1}') for i,model in enumerate(models)]
        if not 1<=len(configs)<=MAX_PLAYERS:raise ValueError('每场需要 1 至 8 位 Agent')
        profiles=[]; ids=set()
        for i,item in enumerate(configs):
            if not isinstance(item,dict):raise ValueError('选手配置必须是对象')
            preset=item.get('preset') or PLAYERS[i%len(PLAYERS)]['id']
            base=next((p for p in PLAYERS if p['id']==preset),None)
            if base is None:raise ValueError('未知的 Agent 风格模板')
            profile=copy.deepcopy(base)
            profile['preset']=preset
            ident=base['id'] if base['id'] not in ids else base['id']+'-'+str(i+1)
            ids.add(ident);profile['id']=ident
            for key,limit in (('name',24),('style',1800)):
                if key in item:
                    val=str(item[key]).strip()
                    if not val or len(val)>limit:raise ValueError(f'选手{key}需要 1 至 {limit} 个字符')
                    profile[key]=val
            profile['model']=item.get('model') or MODELS[0]
            if profile['name'].startswith('deepseek-') and profile['name'][9:].isdigit():
                profile['initial']=profile['name'][9:]
            if 'model' in item and not str(item['model']).strip():raise ValueError('模型名称不能为空')
            validated=self.configuration.normalize_profile({**profile,**{k:v for k,v in item.items() if k in ('connection_id','temperature','max_tokens','max_steps','tools')}})
            profile.update({k:validated[k] for k in ('connection_id','temperature','max_tokens','max_steps','tools','model')})
            if item.get('id'):profile['profile_id']=item['id']
            profiles.append(profile)
        if len({p['name'] for p in profiles})!=len(profiles):
            # Repeated templates are useful, but labels must be unambiguous.
            counts={}
            for p in profiles:
                counts[p['name']]=counts.get(p['name'],0)+1
                if counts[p['name']]>1:p['name']+=f" {counts[p['name']]}"
        return profiles

    async def new(self,mode,start=None,models=None,*,end=None,validation_start=None,players=None,title=None,profile_ids=None):
        await self.stop_auto()
        async with self.lock:
            if mode not in ('live','replay'): raise ValueError('未知比赛模式')
            if profile_ids is not None:
                if players is not None or models is not None:raise ValueError('profile_ids、players、models 只能选择一种选手来源')
                if not profile_ids or len(profile_ids)>8 or len(set(profile_ids))!=len(profile_ids):raise ValueError('请选择 1 至 8 位不重复的 Agent')
                players=[self.configuration.profile(i) for i in profile_ids]
            profiles=self.profiles(players,models)
            dataset=copy.deepcopy(await self.market.dataset())
            dates=dataset['dates']
            cursor=dates.index(start) if start in dates else max(0,len(dates)-21)
            if start and start not in dates: raise ValueError('起始日期不在已加载的真实历史交易日中')
            plan=None
            if mode=='replay':
                start=dates[cursor]; end=end or dates[-1]
                if end not in dates or end<start:raise ValueError('结束日期必须是起始日期之后的可用交易日')
                if validation_start and (validation_start not in dates or not start<validation_start<=end):
                    raise ValueError('验证期起点必须晚于训练起点且不超过结束日期')
                chosen=[d for d in dates if start<=d<=end]
                plan=dict(start=start,end=end,validation_start=validation_start,dates=chosen,total_rounds=len(chosen))
            elif end or validation_start:raise ValueError('实时比赛不接受历史区间')
            day=dates[cursor] if mode=='replay' else now().date().isoformat()
            if mode=='live':
                quotes=await self.market.quotes()
            else: quotes=historical_quotes(dataset,day)
            roster=[]
            for profile in profiles:
                roster.append(dict(profile,cash=INITIAL,positions={},orders=[],notes=[],strategy=profile['style'],
                    versions=[],history=[dict(round=0,date=day,equity=INITIAL)],realized=0,tokens=0,calls=0,
                    summary='等待选手开始独立研究。',peak=INITIAL,drawdown=0))
            state=dict(id=uuid.uuid4().hex[:12],created=timestamp(),mode=mode,round=0,revision=0,day=day,cursor=cursor,
                title=(str(title).strip()[:64] if str(title or '').strip() else ('历史实验' if mode=='replay' else '实时竞技场')+' · '+day),
                players=roster,dataset=dataset,quotes=quotes,benchmark_start=quotes[INDEX]['price'],
                runtime_settings=self.configuration.settings(),
                benchmark=[dict(round=0,date=day,value=0)],watchlist=list(UNIVERSE),last_price_update=timestamp())
            if plan:state['replay_plan']=plan
            self.commit(state); self.error=''; self.initial_error=''; self.phases={}
            return self.public()

    @staticmethod
    def validating(state):
        boundary=state.get('replay_plan',{}).get('validation_start')
        return state['mode']=='replay' and bool(boundary and state['day']>=boundary)

    def capture_checkpoint(self,state,player):
        if player.get('checkpoint'):return
        versions=player['versions']
        observed=[h['date'] for h in player['history'] if h['round']>0 and
            (not state.get('replay_plan',{}).get('validation_start') or h['date']<state['replay_plan']['validation_start'])]
        player['checkpoint']=dict(cutoff=max(observed) if observed else None,
            version=len(versions),strategy=player['strategy'],notes=copy.deepcopy(player['notes']),
            summary=player['summary'],source_versions=copy.deepcopy(versions),source_match_id=state['id'],captured_at=timestamp())

    def learning(self,state=None):
        state=state or self.state
        if state['mode']=='live':
            return dict(stage='live',start=None,end=None,validation_start=None,completed_rounds=state['round'],
                total_rounds=None,progress=None,can_promote=False,promotion_reason='已在实时跟盘模式',metrics=[],
                estimated_max_calls=None,max_calls_per_round=sum(p.get('max_steps',7) for p in state['players']))
        plan=state['replay_plan']; boundary=plan['validation_start']
        complete=state['round']>=plan['total_rounds']
        next_index=min(state['round'],plan['total_rounds']-1)
        next_day=plan['dates'][next_index]
        stage='complete' if complete else ('validation' if boundary and next_day>=boundary else 'training')
        metrics=[]
        for p in state['players']:
            points=[h for h in p['history'] if h['round']>0]
            training=[h for h in points if not boundary or h['date']<boundary]
            validation=[h for h in points if boundary and h['date']>=boundary]
            def segment(rows,baseline,kind):
                peak=baseline; drawdown=0
                for point in rows:
                    peak=max(peak,point['equity']);drawdown=max(drawdown,(1-point['equity']/max(peak,1))*100)
                end_value=rows[-1]['equity'] if rows else baseline
                fills=[o for o in p['orders'] if o['status']=='filled' and
                    (not boundary or o['fill_day']<boundary) == (kind=='training')]
                return dict(rounds=len(rows),return_pct=round((end_value/max(1,baseline)-1)*100,3),
                    max_drawdown_pct=round(drawdown,3),filled_orders=len(fills),start_equity=baseline,
                    end_equity=end_value,from_date=rows[0]['date'] if rows else None,to_date=rows[-1]['date'] if rows else None)
            train=segment(training,INITIAL,'training')
            valid=segment(validation,train['end_equity'],'validation')
            valid['completed_reviews']=len({r['round'] for r in p.get('validation_reviews',[])})
            # This label is evidence availability, never a profitability/stability guarantee.
            evidence='tested' if complete and valid['rounds']>=5 and valid['completed_reviews']>=5 and valid['filled_orders']>=1 and p['versions'] else 'insufficient'
            metrics.append(dict(player_id=p['id'],training=train,validation=valid,evidence=evidence,
                evidence_label='已有独立验证样本' if evidence=='tested' else '验证样本不足',strategy_versions=len(p['versions']),
                trainable=bool(p['versions'])))
        eligible=[p['id'] for p in state['players'] if p['versions']]
        reason='' if complete and eligible else ('请先完成选定的历史区间' if not complete else '没有可迁移的策略复盘，请检查模型调用记录')
        return dict(stage=stage,start=plan['start'],end=plan['end'],validation_start=boundary,
            completed_rounds=min(state['round'],plan['total_rounds']),total_rounds=plan['total_rounds'],
            progress=round(min(1,state['round']/plan['total_rounds'])*100,1),can_promote=complete and bool(eligible),
            promotion_reason=reason,eligible_player_ids=eligible,metrics=metrics,
            estimated_max_calls=max(0,plan['total_rounds']-state['round'])*sum(p.get('max_steps',7) for p in state['players']),
            max_calls_per_round=sum(p.get('max_steps',7) for p in state['players']),frozen_at=boundary if any(p.get('checkpoint') for p in state['players']) else None,
            warning='策略记忆迭代不等于训练模型；验证样本与收益均不构成稳定性保证。')

    async def promote(self,player_ids,title=None):
        await self.stop_auto()
        async with self.lock:
            source=self.state
            if not source or source['mode']!='replay':raise ValueError('只能从历史实验迁移经验')
            info=self.learning(source)
            if not info['can_promote']:raise ValueError(info['promotion_reason'])
            if not player_ids or len(player_ids)>MAX_PLAYERS or len(set(player_ids))!=len(player_ids):
                raise ValueError('请选择 1 至 8 位不重复的 Agent')
            selected=[]
            for ident in player_ids:
                player=next((p for p in source['players'] if p['id']==ident),None)
                if not player:raise ValueError('选中的 Agent 不存在')
                if not player['versions']:raise ValueError('选中的 Agent 尚未形成可迁移的策略经验')
                selected.append(player)
            dataset=copy.deepcopy(await self.market.dataset())
            quotes=await self.market.quotes(); day=now().date().isoformat(); roster=[]
            for original in selected:
                trained=copy.deepcopy(original)
                self.capture_checkpoint(source,trained)
                checkpoint=copy.deepcopy(trained['checkpoint'])
                metric=next(m for m in info['metrics'] if m['player_id']==original['id'])
                provenance=dict(source_match_id=source['id'],source_title=source['title'],source_player_id=original['id'],
                    trained_from=info['start'],trained_until=checkpoint['cutoff'],validation_from=info['validation_start'],
                    validation_until=info['end'] if info['validation_start'] else None,strategy_version=checkpoint['version'],
                    evidence=metric['evidence'],metrics=copy.deepcopy(metric),
                    source_runtime_settings=copy.deepcopy(source.get('runtime_settings',DEFAULT_SETTINGS)),promoted_at=timestamp())
                profile={k:original[k] for k in ('id','name','title','color','initial','style','model','connection_id','temperature','max_tokens','max_steps','tools','profile_id') if k in original}
                roster.append(dict(profile,cash=INITIAL,positions={},orders=[],notes=copy.deepcopy(checkpoint['notes']),strategy=checkpoint['strategy'],
                    versions=[],history=[dict(round=0,date=day,equity=INITIAL)],realized=0,tokens=0,calls=0,
                    summary='已载入历史经验，等待实时行情下的首次独立研究。',peak=INITIAL,drawdown=0,
                    learned_experience=checkpoint,provenance=provenance))
            state=dict(id=uuid.uuid4().hex[:12],created=timestamp(),mode='live',round=0,revision=0,day=day,cursor=0,
                title=(str(title).strip()[:64] if str(title or '').strip() else source['title']+' · 实时跟盘'),players=roster,dataset=dataset,
                quotes=quotes,benchmark_start=quotes[INDEX]['price'],benchmark=[dict(round=0,date=day,value=0)],
                runtime_settings=self.configuration.settings(),
                watchlist=list(UNIVERSE),last_price_update=timestamp(),source_match_id=source['id'])
            self.commit(state);self.error='';self.phases={}
            return self.public()

    def account(self,player,state):
        quotes=state['quotes']; day=state['day']; nav=equity(player,quotes)
        holdings=[]
        for symbol,lots in player['positions'].items():
            qty=quantity(player,symbol); value=cents(quotes[symbol]['price'])*qty if symbol in quotes else sum(l['cost'] for l in lots)
            cost=sum(l['cost'] for l in lots)
            holdings.append(dict(symbol=symbol,name=quotes.get(symbol,{}).get('name',symbol),quantity=qty,
                available=available(player,symbol,day),value=value,cost=cost,pnl=value-cost,weight=round(value/max(1,nav)*100,2)))
        return dict(cash=player['cash'],available_cash=player['cash']-reserved(player),reserved=reserved(player),equity=nav,
            profit=nav-INITIAL,return_pct=round((nav/INITIAL-1)*100,3),holdings=holdings,
            pending=[o for o in player['orders'] if o['status']=='pending'],realized=player['realized'])

    def agent_account(self,player,state):
        account=copy.deepcopy(self.account(player,state))
        for key in ('cash','available_cash','reserved','equity','profit','realized'):account[key]/=100
        for h in account['holdings']:
            for key in ('value','cost','pnl'):h[key]/=100
        for order in account['pending']:
            for key in ('limit','reserve','fill_price','fee'):
                if key in order:order[key]/=100
        return dict(account,money_unit='人民币元 CNY（不是分）',quantity_unit='股')

    async def ensure_asset(self,state,raw):
        symbol=normalize_symbol(raw)
        async with self.asset_lock:
            fetched=state['dataset'].setdefault('bars_fetched',{})
            slot=now().date().isoformat()+('-close' if now().hour>=16 else '-session')
            if (symbol not in state['dataset']['bars'] or
                    (state['mode']=='live' and fetched.get(symbol)!=slot)):
                series=await self.market.bars(symbol)
                quote=await self.market.quotes([symbol])
                if symbol not in quote: raise ValueError('该代码没有可用 A 股报价')
                if state['mode']=='replay' and not any(b['date']<=state['day'] for b in series):
                    raise ValueError('该证券在回放日期没有可用历史数据，不能使用未来上市证券')
                state['dataset']['bars'][symbol]=series
                fetched[symbol]=slot
                state['dataset'].setdefault('names',{})[symbol]=quote[symbol]['name']
                if state['mode']=='live': state['quotes'][symbol]=quote[symbol]
                else: state['quotes'][symbol]=historical_quotes(state['dataset'],state['day'])[symbol]
            if symbol not in state['watchlist']:state['watchlist'].append(symbol)
        return symbol

    async def trace(self,state,run_id,player,tool,title,args,result):
        self.phases[player['id']]=title[:80]
        with self.db:
            self.db.execute('INSERT INTO traces(match_id,run_id,player,round,time,tool,title,args,result,status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (state['id'],run_id,player['id'],state['round'],timestamp(),tool,title[:1600],json.dumps(args,ensure_ascii=False),
                 json.dumps(result,ensure_ascii=False), 'pending'))

    async def execute(self,state,player,sources,name,args):
        if name not in player.get('tools',[t['function']['name'] for t in TOOLS]) and name not in ('finish','get_account'):
            raise ValueError('此 Agent 未获得该工具权限')
        from .research_tools import RESEARCH_TOOL_NAMES,execute_research
        if name in RESEARCH_TOOL_NAMES:
            return await execute_research(self.research,state,player,sources,name,args,policy_frozen=self.validating(state))
        def required(key):
            if key not in args: raise ValueError('缺少参数 '+key)
            return args[key]
        if state['mode']=='replay' and name in ('search_web','read_source','company_profile'):
            raise ValueError('历史回溯禁止现时联网资料，避免未来信息泄漏。可以查截止当前日的price_history并修订策略。')
        if name=='market_scan':
            if state['mode']=='live':return await self.market.scan(args.get('sort','active'))
            return dict(stocks=list(state['quotes'].values()),note='这些是已发现标的的历史行情，不是全市场排行榜。可通过stock_search添加任何A股。')
        if name=='get_account':return self.agent_account(player,state)
        if name=='stock_search':
            return dict(results=await self.market.search(required('query')),note='仅证券标识。现存股票目录存在幸存者偏差，不能视为完整历史证券池。')
        if name=='price_history':
            symbol=await self.ensure_asset(state,required('symbol'))
            visible=[b for b in state['dataset']['bars'][symbol] if b['date']<=state['day']]
            return dict(symbol=symbol,name=state['quotes'][symbol]['name'],columns=['date','open','close','high','low','volume'],
                        bars=[[b[k] for k in ('date','open','close','high','low','volume')] for b in visible[-30:]],quote=state['quotes'][symbol],
                        rules=board_rules(symbol),adjustment='不复权，未处理分红送转；跨除权日比较需谨慎')
        if name=='company_profile': return await self.research.company(normalize_symbol(required('symbol')))
        if name=='search_web':
            result=await self.research.search(required('query'))
            for source in result['sources']:sources[source['id']]=source
            return result
        if name=='read_source':
            source=sources.get(required('source_id'))
            if not source:raise ValueError('只能读取本轮搜索返回的 source_id')
            return await self.research.read(source)
        if name=='place_order':
            symbol=await self.ensure_asset(state,required('symbol'))
            order=submit(player,symbol,required('side'),required('quantity'),required('limit_price'),required('reason'),
                         state['quotes'],state['day'],state['round'],settings=state.get('runtime_settings'))
            model_order=copy.deepcopy(order)
            model_order['limit']/=100;model_order['reserve']/=100
            return dict(order=model_order,money_unit='人民币元 CNY',account=self.agent_account(player,state))
        if name=='cancel_order':
            result=cancel(player,required('order_id'))
            return dict(id=result['id'],status=result['status'],account=self.agent_account(player,state))
        if name=='save_note':
            if self.validating(state):raise ValueError('验证期已冻结训练记忆，不能新增或修改笔记')
            note=str(required('note'))[:1200]
            player['notes']=(player['notes']+[dict(date=state['day'],round=state['round'],note=note)])[-24:]
            return {'saved':note}
        if name=='finish':
            summary=str(required('summary'))[:1600]
            if self.validating(state):
                player.setdefault('validation_reviews',[]).append(dict(round=state['round'],date=state['day'],
                    summary=summary,risk=str(args.get('risk',''))[:600]))
                return dict(summary=summary,strategy_version=len(player['versions']),policy_frozen=True,
                    note='验证期仅保存本轮公开说明与交易记录，不更新策略、复盘版本或笔记')
            strategy=str(args.get('next_strategy') or player['strategy'])[:1600]
            player['strategy']=strategy
            player['versions'].append(dict(version=len(player['versions'])+1,round=state['round'],date=state['day'],
                lesson=str(args.get('lesson','本轮未提供复盘'))[:1000],strategy=strategy,risk=str(args.get('risk',''))[:600],
                equity=equity(player,state['quotes'])))
            return dict(summary=summary,strategy_version=len(player['versions']))
        raise ValueError('未知工具')

    def mark(self,state):
        for p in state['players']:
            nav=equity(p,state['quotes']); p['peak']=max(p['peak'],nav)
            p['drawdown']=max(p['drawdown'],round((1-nav/p['peak'])*100,3))
            point=dict(round=state['round'],date=state['day'],equity=nav)
            if p['history'][-1]['round']==state['round']:p['history'][-1]=point
            else:p['history'].append(point)
        q=state['quotes'].get(INDEX)
        if q:
            point=dict(round=state['round'],date=state['day'],value=round((q['price']/state['benchmark_start']-1)*100,3))
            if state['benchmark'][-1]['round']==state['round']:state['benchmark'][-1]=point
            else:state['benchmark'].append(point)

    async def step(self,request_id):
        async with self.lock:
            if self.db.execute('SELECT 1 FROM commands WHERE request_id=?',(request_id,)).fetchone():return self.public()
            if not self.state:raise ValueError('请先创建比赛')
            if self.state['mode']=='replay' and self.state['round']>=self.state['replay_plan']['total_rounds']:
                raise ValueError('已完成选定历史区间，可以检查验证结果并迁移经验到实时跟盘')
            for player in self.state['players']:
                conn=self.configuration.connection(player.get('connection_id','default'))
                if not conn['has_key'] and not conn['base_url'].startswith(('http://localhost','http://127.0.0.1','http://[::1]')):
                    raise ValueError(f"请先为 {player['name']} 使用的模型连接配置密钥")
            if self.agents.max_calls-self.agents.calls<sum(p.get('max_steps',7) for p in self.state['players']):
                raise ValueError('剩余模型预算不足以保证全体选手完成一轮；请增加调用预算后继续')
            self.busy=True; state=copy.deepcopy(self.state); run_id=uuid.uuid4().hex
            try:
                state['round']+=1;state['revision']+=1
                if state['mode']=='replay':
                    if state['round']>1:state['cursor']+=1
                    if state['cursor']>=len(state['dataset']['dates']):raise ValueError('已到达已加载历史数据末尾，请新建比赛选择更早的日期')
                    state['day']=state['dataset']['dates'][state['cursor']]
                    state['quotes']=historical_quotes(state['dataset'],state['day'])
                    if self.validating(state):
                        for p in state['players']:self.capture_checkpoint(state,p)
                else:
                    state['day']=now().date().isoformat()
                    fresh=await self.market.quotes(list(state['quotes']))
                    state['quotes'].update(fresh);state['last_price_update']=timestamp()
                bars={s:next((b for b in series if b['date']==state['day']),None) for s,series in state['dataset']['bars'].items()}
                for p in state['players']:
                    match_orders(p,state['quotes'],state['day'],state['round'],state['mode'],bars,settings=state.get('runtime_settings'))
                self.mark(state)
                # Freeze identical common observation before concurrent agents discover more assets.
                initial_quotes=copy.deepcopy(list(state['quotes'].values()))
                async def contestant(p):
                    sources={}
                    frozen=self.validating(state)
                    inherited=p.get('learned_experience')
                    if inherited:
                        inherited={k:copy.deepcopy(inherited[k]) for k in ('cutoff','version','strategy','summary','source_match_id')}
                    context=dict(mode=state['mode'],date=state['day'],round=state['round'],account=self.agent_account(p,state),
                        observed_stocks=initial_quotes,notes=p['notes'][-5:],last_review=[{**v,'equity':v['equity']/100} for v in p['versions'][-2:]],
                        performance=[{**h,'equity':h['equity']/100} for h in p['history'][-10:]],max_drawdown=p['drawdown'],
                        previous_summary=p['checkpoint']['summary'] if frozen else p['summary'],
                        learning_phase='validation' if frozen else ('training' if state['mode']=='replay' else 'live'),policy_frozen=frozen,
                        inherited_experience=inherited,
                        runtime_settings=state.get('runtime_settings',DEFAULT_SETTINGS),
                        rules='无股票白名单；先查询真实行情再下单。仓位上限和执行费率以runtime_settings为准。历史模式下轮开盘撮合。')
                    async def execute(name,args):return await self.execute(state,p,sources,name,args)
                    async def emit(tool,title,args,result):await self.trace(state,run_id,p,tool,title,args,result)
                    p['summary']=await self.agents.run(p,context,execute,emit)
                    self.phases[p['id']]='本轮完成'
                tasks=[asyncio.create_task(contestant(p)) for p in state['players']]
                try:
                    await asyncio.gather(*tasks)
                except BaseException:
                    # No sibling may keep writing audit events after the round is aborted.
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    raise
                state['decision_prices']={symbol:q['price'] for symbol,q in state['quotes'].items()}
                state['last_decision_at']=timestamp()
                if state['mode']=='replay' and state['round']>=state['replay_plan']['total_rounds']:
                    for p in state['players']:self.capture_checkpoint(state,p)
                self.mark(state);self.commit(state,request_id,run_id);self.error=''
            except BaseException:
                with self.db:self.db.execute("UPDATE traces SET status='aborted' WHERE run_id=?",(run_id,))
                raise
            finally:self.busy=False
            return self.public()

    async def refresh(self):
        if self.busy or not self.state or self.state['mode']!='live':return
        async with self.lock:
            if not self.state or self.state['mode']!='live':return
            state=copy.deepcopy(self.state)
            fresh=await self.market.quotes(list(state['quotes']))
            if all(state['quotes'].get(symbol)==q for symbol,q in fresh.items()) and state['day']==now().date().isoformat():
                return
            state['quotes'].update(fresh);state['day']=now().date().isoformat();state['last_price_update']=timestamp()
            for p in state['players']:
                match_orders(p,state['quotes'],state['day'],state['round']+1,'live',settings=state.get('runtime_settings'))
            self.mark(state);state['revision']+=1;self.commit(state,checkpoint=False)

    async def price_loop(self):
        while True:
            await asyncio.sleep((self.state or {}).get('runtime_settings',DEFAULT_SETTINGS)['quote_refresh_seconds'])
            try:await self.refresh()
            except Exception:self.error='行情刷新失败，保留带时间戳的旧报价；过期报价不会成交。'

    async def auto(self):
        try:
            if self.state['mode']=='live':
                while self.running:
                    current=self.state
                    settings=current.get('runtime_settings',DEFAULT_SETTINGS)
                    if market_open(current['quotes'].get(INDEX,{})):
                        last=current.get('last_decision_at')
                        elapsed=(now()-datetime.fromisoformat(last)).total_seconds() if last else 1e9
                        reference=current.get('decision_prices',{})
                        move=max((abs(q['price']/reference[symbol]-1) for symbol,q in current['quotes'].items()
                            if reference.get(symbol)),default=0)
                        if elapsed>=settings['decision_interval_seconds'] or (elapsed>=settings['min_decision_gap_seconds'] and move>=settings['decision_move_pct']/100):
                            await self.step(uuid.uuid4().hex)
                    await asyncio.sleep(1)
            else:
                while self.running and self.state['round']<self.state['replay_plan']['total_rounds']:
                    await self.step(uuid.uuid4().hex)
                    await asyncio.sleep(1)
        except Exception as exc:self.error=str(exc) if isinstance(exc,ValueError) else '比赛暂停，当前轮未提交。'
        finally:self.running=False

    def run(self):
        if not self.state:raise ValueError('请先创建实验')
        if self.state['mode']=='replay' and self.state['round']>=self.state['replay_plan']['total_rounds']:
            raise ValueError('选定历史区间已完成')
        self.running=True
        if not self.task or self.task.done():
            self.task=asyncio.create_task(self.auto())

    async def stop_auto(self):
        self.running=False
        if self.task and not self.task.done() and self.task is not asyncio.current_task():
            # Let the current atomic round finish, then wait for the old runner's
            # finally block before replacing the active experiment.
            await asyncio.shield(self.task)

    def extend_budget(self,additional_calls):
        if type(additional_calls) is not int or not 1<=additional_calls<=5000:raise ValueError('每次可增加 1 至 5000 次调用预算')
        self.agents.max_calls+=additional_calls
        self.error=''
        return self.public()

    def public(self):
        connections={c['id']:c for c in self.configuration.connections()}
        def ready(connection_id):
            connection=connections.get(connection_id,{})
            return bool(connection.get('has_key') or connection.get('base_url','').startswith('http://'))
        configured=all(ready(p.get('connection_id','default')) for p in self.state['players']) if self.state else any(ready(i) for i in connections)
        runtime=dict(busy=self.busy,running=self.running,error=self.error or self.initial_error,phases=self.phases,
            configured=configured,calls=self.agents.calls,tokens=self.agents.tokens,failures=self.agents.failures,
            inflight=self.agents.inflight,concurrency=self.agents.concurrency,max_calls=self.agents.max_calls,
            remaining_calls=max(0,self.agents.max_calls-self.agents.calls))
        if not self.state:return {'ready':False,'runtime':runtime}
        s=self.state
        players=[]
        learning=self.learning(s)
        for p in s['players']:
            players.append({**{k:v for k,v in p.items() if k not in ('positions','cash')},**self.account(p,s),
                'learning_metric':next((m for m in learning['metrics'] if m['player_id']==p['id']),None)})
        trace_rows=self.db.execute('SELECT id,player,round,time,tool,title,status,result FROM traces WHERE match_id=? ORDER BY id DESC LIMIT 90',(s['id'],)).fetchall()
        traces=[]
        for row in trace_rows:
            item=dict(zip(('id','player','round','time','tool','title','status'),row[:7]))
            result=json.loads(row[7]);item['error']=result.get('error') if isinstance(result,dict) else None
            traces.append(item)
        return dict(ready=True,id=s['id'],mode=s['mode'],round=s['round'],day=s['day'],revision=s['revision'],
            title=s['title'],learning=learning,source_match_id=s.get('source_match_id'),
            runtime_settings=s.get('runtime_settings',DEFAULT_SETTINGS),
            players=players,quotes=list(s['quotes'].values()),benchmark=s['benchmark'],traces=traces,runtime=runtime,
            dates=s['dataset']['dates'],source=s['dataset']['source'],fetched_at=s['dataset']['fetched_at'],
            last_price_update=s['last_price_update'],market_open=market_open(s['quotes'].get(INDEX,{})),
            index_bars=[b for b in s['dataset']['bars'][INDEX] if b['date']<=s['day']][-45:])

    def trace_detail(self,ident):
        row=self.db.execute('SELECT tool,title,args,result,status,time FROM traces WHERE id=? AND match_id=?',(ident,self.state['id'])).fetchone()
        if not row:raise ValueError('记录不存在')
        return dict(tool=row[0],title=row[1],args=json.loads(row[2]),result=json.loads(row[3]),status=row[4],time=row[5])

    async def load(self,ident):
        await self.stop_auto()
        async with self.lock:
            row=self.db.execute('SELECT payload FROM matches WHERE id=?',(ident,)).fetchone()
            if not row:raise ValueError('比赛不存在')
            self.state=self.migrate(json.loads(row[0]));self.phases={};self.error=''
            with self.db:self.db.execute("INSERT OR REPLACE INTO settings VALUES('active',?)",(ident,))
            return self.public()

    async def rename(self,ident,title):
        if not isinstance(title,str) or not title.strip() or len(title)>64:raise ValueError('实验名称需要 1 至 64 个字符')
        async with self.lock:
            row=self.db.execute('SELECT payload FROM matches WHERE id=?',(ident,)).fetchone()
            if not row:raise ValueError('实验不存在')
            state=self.migrate(json.loads(row[0]));state['title']=title.strip();state['revision']+=1
            if self.state and self.state['id']==ident:self.commit(state)
            else:
                with self.db:self.db.execute('UPDATE matches SET payload=? WHERE id=?',(json.dumps(state,ensure_ascii=False),ident))
            return dict(id=ident,title=state['title'])

    async def close(self):
        self.running=False
        await self.market_monitor.close()
        for task in (self.task,self.ticker):
            if task:
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
        for client in (self.market.client,self.research.client,self.agents.client):await client.aclose()
        self.db.close()
