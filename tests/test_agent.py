import json
import httpx
import pytest
from app.agent import Agents
from app.config import PLAYERS

async def test_real_tool_loop_observes_results_and_revises_strategy():
    agent=Agents();agent.key='test';calls=0;observed=[]
    async def respond(request):
        nonlocal calls
        body=json.loads(request.content);calls+=1
        if calls==1:name,args='market_scan',{}
        else:
            assert body['messages'][-1]['role']=='tool'
            assert 'fixture quote' in body['messages'][-1]['content']
            name,args='finish',dict(summary='完成',risk='未知',lesson='观察了反馈',next_strategy='下一轮等待回调')
        return httpx.Response(200,json={'choices':[{'message':{'tool_calls':[{'id':str(calls),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}], 'usage':{'total_tokens':100}})
    await agent.client.aclose();agent.client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    async def execute(name,args):
        observed.append((name,args))
        return {'quote':'fixture quote'} if name=='market_scan' else {'summary':args['summary']}
    async def emit(*args):pass
    p=dict(PLAYERS[0],model='deepseek-flash',strategy='initial',tokens=0,calls=0)
    try:
        await agent.run(p,{},execute,emit)
        assert [r[0] for r in observed]==['market_scan','finish'] and p['tokens']==200
    finally:await agent.client.aclose()
