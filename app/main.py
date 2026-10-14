import asyncio
import json
import os
from contextlib import asynccontextmanager
from pydantic import BaseModel,Field
from typing import Literal
from fastapi import FastAPI,Request,HTTPException,Body
from fastapi.responses import FileResponse,JSONResponse,StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from .hosting import install_hosting
from .config import ROOT,PLAYERS,MODELS,MAX_PLAYERS
from .engine import Arena
from .configuration import LIMITS
from .agent import TOOLS
from .research_api import router as research_router

@asynccontextmanager
async def lifespan(app):
    app.state.arena=Arena(os.getenv('ARENA_DB'))
    await app.state.arena.initialize()
    yield
    await app.state.arena.close()

app=FastAPI(title='Alpha Arena · A股 Agent 竞技场',lifespan=lifespan)
app.include_router(research_router)
install_hosting(app)
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')

@app.exception_handler(ValueError)
async def invalid(request,exc):return JSONResponse({'detail':str(exc)},status_code=400)

@app.exception_handler(RequestValidationError)
async def invalid_request(request,exc):
    # FastAPI's default validation response echoes submitted values. A malformed
    # connection request must never reflect an API key back to the browser/log.
    return JSONResponse({'detail':[{'loc':e['loc'],'msg':e['msg'],'type':e['type']} for e in exc.errors()]},status_code=422)

class PlayerConfig(BaseModel):
    preset:str|None=None
    name:str|None=Field(default=None,min_length=1,max_length=24)
    style:str|None=Field(default=None,min_length=1,max_length=1800)
    model:str=Field(default='deepseek-flash',min_length=1,max_length=160)
    connection_id:str='default'
    temperature:float=Field(default=.65,ge=0,le=2)
    max_tokens:int=Field(default=1100,ge=256,le=8192,strict=True)
    max_steps:int=Field(default=7,ge=2,le=12,strict=True)
    tools:list[str]|None=None
class New(BaseModel):
    mode:Literal['live','replay']='live'
    title:str|None=Field(default=None,max_length=64)
    start:str|None=None
    end:str|None=None
    validation_start:str|None=None
    models:list[str]|None=Field(default=None,min_length=1,max_length=8)
    players:list[PlayerConfig]|None=Field(default=None,min_length=1,max_length=8)
    profile_ids:list[str]|None=Field(default=None,min_length=1,max_length=8)
class Command(BaseModel):request_id:str=Field(min_length=8,max_length=80)
class Promote(BaseModel):
    player_ids:list[str]=Field(min_length=1,max_length=8)
    title:str|None=Field(default=None,max_length=64)
class Budget(BaseModel):additional_calls:int=Field(ge=1,le=5000,strict=True)

@app.get('/')
async def home():return FileResponse(ROOT/'static/index.html')
@app.get('/api/state')
async def state(request:Request):return request.app.state.arena.public()
@app.get('/api/config')
async def config(request:Request):
    cfg=request.app.state.arena.configuration
    return dict(presets=PLAYERS,models=sorted({m for c in cfg.connections() for m in c['models']}),max_players=MAX_PLAYERS,
        initial_capital=1000000,max_calls_per_player_round=12,connections=cfg.connections(),profiles=cfg.profiles(),
        tool_catalog=[dict(name=t['function']['name'],description=t['function']['description'],required=t['function']['name'] in ('finish','get_account')) for t in TOOLS],
        runtime_settings=cfg.settings(),runtime_schema={k:dict(min=v[0],max=v[1]) for k,v in LIMITS.items()},runtime_scope='new_experiments')

@app.get('/api/market/live')
async def market_live(request:Request,refresh:bool=False):
    return await request.app.state.arena.market_monitor.refresh(force=refresh)

@app.get('/api/market/stocks/{symbol}')
async def stock_detail(symbol:str,request:Request):
    return await request.app.state.arena.market_monitor.detail(symbol)

@app.get('/api/market/search')
async def market_search(request:Request,q:str=''):
    return await request.app.state.arena.market_monitor.search(q)

@app.post('/api/market/watchlist')
async def watch_stock(request:Request,body:dict=Body(...)):
    return await request.app.state.arena.market_monitor.add(body.get('symbol',''))

@app.delete('/api/market/watchlist/{symbol}')
async def unwatch_stock(symbol:str,request:Request):
    return await request.app.state.arena.market_monitor.remove(symbol)
@app.post('/api/matches')
async def new(body:New,request:Request):
    return await request.app.state.arena.new(body.mode,body.start,body.models,end=body.end,
        validation_start=body.validation_start,players=[p.model_dump(exclude_none=True) for p in body.players] if body.players else None,title=body.title,profile_ids=body.profile_ids)

@app.get('/api/connections')
async def connections(request:Request):return request.app.state.arena.configuration.connections()
@app.post('/api/connections')
async def add_connection(request:Request,body:dict=Body(...)):return request.app.state.arena.configuration.save_connection(body)
@app.put('/api/connections/{ident}')
async def update_connection(ident:str,request:Request,body:dict=Body(...)):
    return request.app.state.arena.configuration.save_connection(dict(body,_update=True),ident)
@app.delete('/api/connections/{ident}')
async def remove_connection(ident:str,request:Request):
    request.app.state.arena.configuration.delete_connection(ident);return {'deleted':True}
@app.post('/api/connections/{ident}/test')
@app.get('/api/connections/{ident}/models')
async def test_connection(ident:str,request:Request):
    arena=request.app.state.arena;cfg=arena.configuration;connection=cfg.connection(ident,secret=True)
    key=connection['api_key'];headers={'Authorization':'Bearer '+key} if key else {}
    try:
        response=await arena.agents.client.get(connection['base_url']+'/models',headers=headers,timeout=20)
        if response.status_code in (401,403):return dict(ok=False,models=[],message='连接已到达，密钥未通过验证')
        if response.status_code==404:return dict(ok=False,models=connection['models'],message='服务未提供 /models 接口，可手工填写模型名称')
        response.raise_for_status();data=response.json()
        rows=data.get('data',[]) if isinstance(data,dict) else []
        models=[m['id'].strip() for m in rows if isinstance(m,dict) and isinstance(m.get('id'),str) and 0<len(m['id'])<=160 and (not key or key not in m['id'])][:200]
        if request.method=='POST':cfg.save_connection({'models':models},ident)
        return dict(ok=True,models=models,message='连接成功，已读取模型列表；未调用模型生成接口')
    except Exception:return dict(ok=False,models=[],message='连接检查失败，请检查地址、网络和服务状态')
@app.get('/api/profiles')
async def profiles(request:Request):return request.app.state.arena.configuration.profiles()
@app.post('/api/profiles')
async def add_profile(request:Request,body:dict=Body(...)):return request.app.state.arena.configuration.save_profile(body)
@app.put('/api/profiles/{ident}')
async def update_profile(ident:str,request:Request,body:dict=Body(...)):
    cfg=request.app.state.arena.configuration;cfg.profile(ident);return cfg.save_profile(body,ident)
@app.delete('/api/profiles/{ident}')
async def remove_profile(ident:str,request:Request):
    request.app.state.arena.configuration.delete_profile(ident);return {'deleted':True}
@app.get('/api/settings')
async def settings(request:Request):return request.app.state.arena.configuration.settings()
@app.post('/api/settings')
@app.put('/api/settings')
async def update_settings(request:Request,body:dict=Body(...)):return request.app.state.arena.configuration.save_settings(body)
@app.post('/api/matches/{ident}/rename')
async def rename(ident:str,request:Request,body:dict=Body(...)):return await request.app.state.arena.rename(ident,body.get('title'))
@app.get('/api/matches')
async def matches(request:Request):
    arena=request.app.state.arena; output=[]
    for row in arena.db.execute('SELECT payload FROM matches ORDER BY created DESC LIMIT 100'):
        saved=arena.migrate(json.loads(row[0]));info=arena.learning(saved)
        output.append(dict(id=saved['id'],created=saved['created'],mode=saved['mode'],title=saved['title'],
            round=saved['round'],day=saved['day'],player_count=len(saved['players']),
            learning={k:info[k] for k in ('stage','completed_rounds','total_rounds','progress','can_promote')}))
    return output
@app.post('/api/promote')
async def promote(body:Promote,request:Request):return await request.app.state.arena.promote(body.player_ids,body.title)
@app.post('/api/budget')
async def budget(body:Budget,request:Request):return request.app.state.arena.extend_budget(body.additional_calls)
@app.post('/api/matches/{ident}/load')
async def load(ident:str,request:Request):return await request.app.state.arena.load(ident)
@app.post('/api/step')
async def step(body:Command,request:Request):
    arena=request.app.state.arena
    if arena.busy or arena.running:raise HTTPException(409,'选手正在决策，请等待本轮完成')
    return await arena.step(body.request_id)
@app.post('/api/run')
async def run(request:Request):request.app.state.arena.run();return {'running':True}
@app.post('/api/pause')
async def pause(request:Request):request.app.state.arena.running=False;return {'running':False}
@app.post('/api/refresh')
async def refresh(request:Request):
    await request.app.state.arena.refresh();return request.app.state.arena.public()
@app.get('/api/traces/{ident}')
async def trace(ident:int,request:Request):return request.app.state.arena.trace_detail(ident)
@app.get('/api/export')
async def export(request:Request):
    a=request.app.state.arena
    ids=[r[0] for r in a.db.execute('SELECT id FROM traces WHERE match_id=? ORDER BY id',(a.state['id'],))]
    return JSONResponse(dict(state=a.public(),traces=[a.trace_detail(i) for i in ids]),
        headers={'Content-Disposition':f'attachment; filename="arena-{a.state["id"]}.json"'})
@app.get('/api/stream')
async def stream(request:Request):
    async def generate():
        last='';last_market=-1;ticks=0
        while not await request.is_disconnected():
            monitor=request.app.state.arena.market_monitor
            if monitor.revision!=last_market:
                yield 'event: market\ndata: '+json.dumps(monitor.public(),ensure_ascii=False,separators=(',',':'))+'\n\n'
                last_market=monitor.revision
            data=json.dumps(request.app.state.arena.public(),ensure_ascii=False,separators=(',',':'))
            if data!=last:yield 'event: state\ndata: '+data+'\n\n';last=data
            elif ticks%20==0:yield ': heartbeat\n\n'
            ticks+=1;await asyncio.sleep(.5)
    return StreamingResponse(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache'})

@app.get('/healthz', include_in_schema=False)
async def healthz():
    return {'status': 'ok'}
