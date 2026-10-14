"""Local research workbench: source inspection, uploads and harmless tool probes."""
import base64
import binascii
from typing import Literal
from fastapi import APIRouter, Request, Query
from pydantic import BaseModel, Field
from .config import now
from .research import MAX_DOWNLOAD
from .evidence import MAX_TEXT, publication_date
from .research_tools import RESEARCH_TOOL_NAMES, execute_research

router = APIRouter(prefix='/api',tags=['研究资料'])


class EvidenceImport(BaseModel):
    title:str=Field(min_length=1,max_length=240)
    text:str|None=Field(default=None,max_length=MAX_TEXT)
    pdf_base64:str|None=Field(default=None,max_length=16_000_008)
    url:str|None=Field(default=None,max_length=2000)
    published_at:str|None=None
    company:str=Field(default='',max_length=120)
    topic:str=Field(default='',max_length=180)
    point_in_time_admissible:bool=False
    publication_basis:str=Field(default='',max_length=1000)


class EvidenceQuery(BaseModel):
    query:str=Field(default='',max_length=200)
    company:str|None=Field(default=None,max_length=120)
    as_of:str|None=None
    limit:int=Field(default=20,ge=1,le=50)


class ToolTest(BaseModel):
    tool:str=Field(min_length=1,max_length=60)
    args:dict=Field(default_factory=dict)
    as_of:str|None=None


@router.get('/research/tools')
async def catalog(request:Request,mode:Literal['live','replay']='live'):
    research=request.app.state.arena.research
    return dict(tools=research.catalog(mode),mode=mode,
        note='未验证表示本服务尚未成功调用；可用状态不代表供应商保证。独立工具测试不消耗模型额度。')


@router.get('/evidence')
async def evidence_list(request:Request,query:str='',company:str|None=None,as_of:str|None=None,
                        limit:int=Query(default=20,ge=1,le=50)):
    return request.app.state.arena.research.evidence.search(query,company=company,as_of=as_of,limit=limit)


@router.get('/evidence/search')
async def evidence_search_get(request:Request,query:str='',company:str|None=None,as_of:str|None=None,
                             limit:int=Query(default=20,ge=1,le=50)):
    return request.app.state.arena.research.evidence.search(query,company=company,as_of=as_of,limit=limit)


@router.post('/evidence/search')
async def evidence_search(body:EvidenceQuery,request:Request):
    return request.app.state.arena.research.evidence.search(**body.model_dump())


@router.post('/evidence/import')
async def evidence_import(body:EvidenceImport,request:Request):
    raw=None
    if body.pdf_base64 is not None:
        try:
            raw=base64.b64decode(body.pdf_base64,validate=True)
        except (ValueError,binascii.Error) as exc:
            raise ValueError('PDF 文件编码无效，请重新选择文件') from exc
        if len(raw)>MAX_DOWNLOAD:
            raise ValueError('PDF 文件不能超过 12 MB')
    return await request.app.state.arena.research.import_document(title=body.title,text=body.text,pdf=raw,
        url=body.url,published_at=body.published_at,company=body.company,topic=body.topic,
        point_in_time_admissible=body.point_in_time_admissible,publication_basis=body.publication_basis)


@router.get('/evidence/{document_id}')
async def evidence_detail(document_id:str,request:Request,as_of:str|None=None):
    return request.app.state.arena.research.evidence.get(document_id,as_of=as_of)


@router.post('/research/tools/test')
async def test_tool(body:ToolTest,request:Request):
    research=request.app.state.arena.research
    args=body.args
    name=body.tool
    cutoff=publication_date(body.as_of) if body.as_of is not None else None
    if body.as_of is not None and not cutoff:
        raise ValueError('历史工具测试必须提供明确的回溯日期')
    # This is a user-operated research workspace, separate from an Agent account.
    # An explicit as_of turns it into an offline historical probe.
    state=dict(mode='replay' if cutoff else 'live',day=cutoff or now().date().isoformat(),round=0)
    if name=='save_research_note':
        raise ValueError('工具测试不能修改 Agent 记忆，请在比赛内由选手保存可追溯笔记')
    try:
        if name in RESEARCH_TOOL_NAMES:
            result=await execute_research(research,state,{'notes':[]},research.sources,name,args)
        elif name in ('search_web','company_profile','read_source'):
            if cutoff:
                raise ValueError('历史工具测试禁止当前联网，请检索已归档且符合时点的资料')
            if name=='search_web':
                result=await research.search(args.get('query',''))
            elif name=='company_profile':
                result=await research.company(args.get('symbol',''))
            else:
                source=research.sources.get(args.get('source_id'))
                if not source:
                    raise ValueError('请先搜索，并使用本服务返回的 source_id；重启后需重新搜索未读取来源')
                result=await research.read(source)
        else:
            raise ValueError('仅支持无交易副作用的研究工具测试')
        research.observed(name)
        return dict(tool=name,result=result,as_of=cutoff,
            context='独立研究工具测试；不调用模型、不改变账户。读取原文会持久归档。历史 Agent 仍执行自己的时点限制。')
    except Exception as exc:
        research.observed(name,exc)
        if isinstance(exc,ValueError):
            raise
        raise ValueError('研究工具调用失败：'+str(exc)[:240]) from exc
