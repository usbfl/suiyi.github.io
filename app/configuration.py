"""User-owned connections, reusable Agent profiles and effective runtime settings."""
import copy
import json
import math
import os
import uuid
from urllib.parse import urlparse
from .config import PLAYERS,MODELS,timestamp,use_agent_code
from .secrets import seal,unseal

DEFAULT_SETTINGS=dict(max_position_pct=45,slippage_bps=5,commission_bps=3,minimum_commission_yuan=5,
    quote_refresh_seconds=30,decision_interval_seconds=900,decision_move_pct=.6,min_decision_gap_seconds=60)
LIMITS=dict(max_position_pct=(1,100),slippage_bps=(0,100),commission_bps=(0,100),minimum_commission_yuan=(0,100),
    quote_refresh_seconds=(5,3600),decision_interval_seconds=(30,86400),decision_move_pct=(.01,30),min_decision_gap_seconds=(10,86400))

class Configuration:
    def __init__(self,db,tools):
        self.db=db;self.tools=tools
        db.executescript('''CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY,payload TEXT NOT NULL,secret TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_profiles(id TEXT PRIMARY KEY,payload TEXT NOT NULL);''')
        if not db.execute('SELECT 1 FROM connections WHERE id=?',('default',)).fetchone():
            self.save_connection(dict(name='默认 DeepSeek',adapter='deepseek',base_url=os.getenv('LLM_BASE_URL','https://api.deepseek.com'),
                models=MODELS,api_key=os.getenv('LLM_API_KEY','')),ident='default',creating=True)
        if not db.execute('SELECT 1 FROM settings WHERE key=?',('profiles_seeded',)).fetchone():
            for preset in PLAYERS:
                self.save_profile(dict(name=preset['name'],style=preset['style'],preset=preset['id'],connection_id='default',model=MODELS[0]),ident=preset['id'])
            with db:db.execute("INSERT INTO settings VALUES('profiles_seeded','1')")
        for ident,payload in db.execute('SELECT id,payload FROM agent_profiles').fetchall():
            profile=json.loads(payload)
            if use_agent_code(profile):
                with db:db.execute('UPDATE agent_profiles SET payload=? WHERE id=?',
                    (json.dumps(profile,ensure_ascii=False),ident))

    def connections(self):
        return [dict(json.loads(r[1]),has_key=bool(r[2])) for r in self.db.execute('SELECT id,payload,secret FROM connections ORDER BY rowid')]

    def connection(self,ident='default',secret=False):
        row=self.db.execute('SELECT payload,secret FROM connections WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('模型连接不存在')
        result=dict(json.loads(row[0]),has_key=bool(row[1]))
        if secret:result['api_key']=unseal(row[1])
        return result

    def save_connection(self,data,ident=None,creating=False):
        ident=ident or uuid.uuid4().hex[:12]
        old=self.db.execute('SELECT payload,secret FROM connections WHERE id=?',(ident,)).fetchone()
        if not old and not creating and data.get('_update'):raise ValueError('模型连接不存在')
        result=json.loads(old[0]) if old else {}
        result.update({k:v for k,v in data.items() if k in ('name','adapter','base_url','models')})
        name=str(result.get('name','')).strip();adapter=result.get('adapter','openai_compatible')
        base=str(result.get('base_url','')).rstrip('/');parsed=urlparse(base)
        if not name or len(name)>60:raise ValueError('连接名称需要 1 至 60 个字符')
        if adapter not in ('deepseek','openai_compatible'):raise ValueError('不支持的模型适配器')
        if (parsed.scheme!='https' and not(parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1','::1'))) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('连接地址需要 HTTPS；本机模型可使用 http://localhost 或 127.0.0.1。地址不能含密钥或查询参数')
        models=result.get('models',[])
        if not isinstance(models,list) or len(models)>200 or any(not isinstance(m,str) or not m.strip() or len(m)>160 for m in models):raise ValueError('模型列表格式无效')
        result=dict(id=ident,name=name,adapter=adapter,base_url=base,models=list(dict.fromkeys(m.strip() for m in models)),updated_at=timestamp())
        encrypted=old[1] if old else ''
        if 'api_key' in data:
            key=data['api_key']
            if not isinstance(key,str) or len(key)>4096:raise ValueError('密钥格式无效')
            key=key.strip()
            if key and any(ord(c)<33 or ord(c)>126 for c in key):raise ValueError('密钥不能包含空格、换行或非 ASCII 字符')
            if key:encrypted=seal(key)
        if data.get('clear_key') is True:encrypted=''
        with self.db:self.db.execute('INSERT OR REPLACE INTO connections VALUES(?,?,?)',(ident,json.dumps(result,ensure_ascii=False),encrypted))
        return dict(result,has_key=bool(encrypted))

    def delete_connection(self,ident):
        if ident=='default':raise ValueError('默认连接用于兼容历史实验，可修改但不能删除')
        if any(p['connection_id']==ident for p in self.profiles()):raise ValueError('请先修改或删除使用该连接的 Agent 配置')
        for (payload,) in self.db.execute('SELECT payload FROM matches'):
            if any(p.get('connection_id','default')==ident for p in json.loads(payload)['players']):
                raise ValueError('已有实验使用此连接，请保留连接以便重放；可以更新密钥')
        with self.db:self.db.execute('DELETE FROM connections WHERE id=?',(ident,))

    def normalize_profile(self,data):
        result=copy.deepcopy(data)
        for key,maxlen in [('name',24),('style',1800),('model',160)]:
            value=result.get(key,'')
            if not isinstance(value,str) or not value.strip() or len(value)>maxlen:raise ValueError(f'{key}需要 1 至 {maxlen} 个字符')
            result[key]=value.strip()
        result['connection_id']=result.get('connection_id') or 'default';self.connection(result['connection_id'])
        for key,default,low,high in [('temperature',.65,0,2),('max_tokens',1100,256,8192),('max_steps',7,2,12)]:
            value=result.get(key,default)
            if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high:raise ValueError(f'{key}超出允许范围 {low}–{high}')
            if key!='temperature' and type(value) is not int:raise ValueError(f'{key}必须是整数')
            result[key]=value
        enabled=result.get('tools',list(self.tools()))
        if not isinstance(enabled,list) or any(t not in self.tools() for t in enabled):raise ValueError('工具权限列表包含未知工具')
        result['tools']=list(dict.fromkeys(enabled+['finish','get_account']))
        return {k:v for k,v in result.items() if k in ('name','style','model','connection_id','temperature','max_tokens','max_steps','tools','preset','id')}

    def profiles(self):return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM agent_profiles ORDER BY rowid')]

    def profile(self,ident):
        row=self.db.execute('SELECT payload FROM agent_profiles WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('Agent 配置不存在')
        return json.loads(row[0])

    def save_profile(self,data,ident=None):
        ident=ident or uuid.uuid4().hex[:12]
        old=self.db.execute('SELECT payload FROM agent_profiles WHERE id=?',(ident,)).fetchone()
        merged=json.loads(old[0]) if old else {};merged.update(data)
        result=dict(self.normalize_profile(merged),id=ident,updated_at=timestamp())
        with self.db:self.db.execute('INSERT OR REPLACE INTO agent_profiles VALUES(?,?)',(ident,json.dumps(result,ensure_ascii=False)))
        return result

    def delete_profile(self,ident):
        with self.db:self.db.execute('DELETE FROM agent_profiles WHERE id=?',(ident,))

    def settings(self):
        row=self.db.execute("SELECT value FROM settings WHERE key='runtime_settings'").fetchone()
        return dict(DEFAULT_SETTINGS,**(json.loads(row[0]) if row else {}))

    def save_settings(self,data):
        if any(k not in LIMITS for k in data):raise ValueError('未知运行参数')
        result=self.settings()
        for key,value in data.items():
            low,high=LIMITS[key]
            if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high:raise ValueError(f'{key}超出允许范围 {low}–{high}')
            result[key]=value
        if result['min_decision_gap_seconds']>result['decision_interval_seconds']:raise ValueError('最短决策间隔不能超过定时决策间隔')
        with self.db:self.db.execute("INSERT OR REPLACE INTO settings VALUES('runtime_settings',?)",(json.dumps(result),))
        return result
