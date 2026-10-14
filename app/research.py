"""Public research adapters and a durable, source-backed evidence archive."""
import asyncio
import hashlib
import io
import ipaddress
import json
import re
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from pypdf import PdfReader
import httpx
from .config import timestamp, normalize_symbol, now, CST
from .evidence import EvidenceStore, MAX_TEXT, publication_date, query_terms

ALLOWED = ('eastmoney.com','dfcfw.com','sina.com.cn','cninfo.com.cn','sse.com.cn','szse.cn',
           'bse.cn','stcn.com','cs.com.cn','stats.gov.cn','miit.gov.cn','gov.cn','pbc.gov.cn',
           'csrc.gov.cn','caam.org.cn','catarc.ac.cn','moutaichina.com','midea.com',
           'byd.com','bydglobal.com','cmbchina.com','pingan.com','ctg.com.cn','cypc.com.cn',
           'zijinmining.com','citics.com')
MAX_DOWNLOAD = 12_000_000


def observe_failures(name):
    def decorate(function):
        @wraps(function)
        async def wrapped(self,*args,**kwargs):
            try:
                return await function(self,*args,**kwargs)
            except Exception as exc:
                self.observed(name,exc)
                raise
        return wrapped
    return decorate


def allowed_url(url):
    try:
        p = urlparse(str(url))
        return (p.scheme == 'https' and not p.username and not p.password and p.port in (None,443)
                and any(p.hostname == domain or (p.hostname or '').endswith('.'+domain) for domain in ALLOWED))
    except ValueError:
        return False


def extract_pdf(payload):
    if not bytes(payload).lstrip().startswith(b'%PDF-'):
        raise ValueError('来源未返回有效 PDF，可能需要登录或触发反爬；请使用其他来源或上传原始文件')
    try:
        reader = PdfReader(io.BytesIO(payload))
        if reader.is_encrypted:
            raise ValueError('不支持加密 PDF，请上传可读取的公开资料')
        page_count = len(reader.pages)
        pieces = []
        for index, page in enumerate(reader.pages[:40]):
            pieces.append(f'\n[第 {index+1} 页]\n'+(page.extract_text() or ''))
            if sum(map(len,pieces)) >= MAX_TEXT:
                break
        text = '\n'.join(pieces)[:MAX_TEXT]
        if len(re.sub(r'\[第 \d+ 页\]|\s','',text)) < 20:
            raise ValueError('PDF 无可提取正文，扫描文件需要先 OCR；未生成虚构文本')
        return text, dict(format='pdf',pages=page_count,pages_extracted=len(pieces),
                          truncated=page_count>len(pieces) or len(text)>=MAX_TEXT)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('PDF 解析失败，请使用公开可搜索文本版文件') from exc


class Research:
    def __init__(self, db=None):
        self.client = httpx.AsyncClient(timeout=20,follow_redirects=False,
            limits=httpx.Limits(max_connections=6,max_keepalive_connections=6),
            headers={'User-Agent':'Mozilla/5.0','Accept-Language':'zh-CN,zh;q=0.9'})
        self.cache = {}
        self.sources = {}
        self.evidence = EvidenceStore(db)
        self.health = {}
        self._directory = None
        self._report_cache = None
        self._statistics_cache = None
        self.sem = asyncio.Semaphore(3)

    def register(self, result):
        for source in result.get('sources',[]):
            self.sources[source['id']] = source
        while len(self.sources)>500:
            self.sources.pop(next(iter(self.sources)))
        return result

    def observed(self, tool, error=None):
        current = self.health.setdefault(tool,{})
        current['checked_at'] = timestamp()
        if error:
            current['status'] = 'error'
            current['last_error'] = str(error)[:350]
        else:
            current.update(status='verified',last_success=timestamp(),last_error=None)

    def catalog(self, mode='live'):
        from .research_tools import tool_catalog
        return tool_catalog(self,mode)

    @observe_failures('search_web')
    async def search(self, query):
        query = str(query).strip()[:160]
        if not query:
            raise ValueError('搜索内容为空')
        r = await self.client.get('https://www.bing.com/search',params={'q':query,'format':'rss'})
        for _ in range(3):
            if not r.is_redirect:
                break
            target = urljoin(str(r.url),r.headers.get('location',''))
            parsed = urlparse(target)
            if parsed.scheme!='https' or parsed.hostname not in ('www.bing.com','cn.bing.com'):
                raise ValueError('搜索服务返回了不支持的重定向')
            r = await self.client.get(target)
        r.raise_for_status()
        if len(r.content)>1_000_000:
            raise ValueError('搜索结果超出大小限制')
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as exc:
            raise ValueError('搜索服务未返回 RSS，可能限流或要求验证') from exc
        sources = []
        for item in root.findall('.//item')[:8]:
            url = item.findtext('link','')
            if urlparse(url).scheme not in ('http','https'):
                continue
            sources.append(dict(id=hashlib.sha256(url.encode()).hexdigest()[:16],
                title=item.findtext('title','')[:180],url=url,
                snippet=BeautifulSoup(item.findtext('description',''),'html.parser').get_text(' ',strip=True)[:650],
                published_at=None,fetched_at=timestamp(),readable=allowed_url(url),
                source_kind='web_search',point_in_time_admissible=False,publication_basis='',
                note='RSS 时间可能是索引时间；不作为原始发布日期'))
        self.observed('search_web')
        return self.register(dict(query=query,sources=sources,
            note='搜索摘要不是已核实事实；阅读原文后才能保存原文引用。发布日期未知的资料不能进入历史回放。'))

    @observe_failures('company_profile')
    async def company(self, symbol):
        symbol = normalize_symbol(symbol)
        code = symbol[2:]+'.'+symbol[:2].upper()
        url = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
        r = await self.client.get(url,params={'reportName':'RPT_F10_ORG_BASICINFO','columns':'ALL',
            'filter':f'(SECUCODE="{code}")','pageSize':1,'pageNumber':1})
        r.raise_for_status()
        records = (r.json().get('result') or {}).get('data') or []
        if not records:
            raise ValueError('公司资料暂不可用')
        row = records[0]
        result = dict(symbol=symbol,name=row.get('ORG_NAME'),industry=row.get('EM2016'),
            business=str(row.get('MAIN_BUSINESS',''))[:1800],profile=str(row.get('ORG_PROFIE',''))[:2200],
            website=row.get('ORG_WEB'),source=f'https://quote.eastmoney.com/{symbol}.html',
            fetched_at=timestamp(),published_at=None,
            note='第三方当前公司资料；请核对原始公告，不能作为历史时点资料。')
        self.observed('company_profile')
        return result

    @observe_failures('search_company_filings')
    async def search_company_filings(self, symbol, query=''):
        symbol = normalize_symbol(symbol)
        if self._directory is None:
            r = await self.client.get('https://www.cninfo.com.cn/new/data/szse_stock.json')
            r.raise_for_status()
            self._directory = {x.get('code'):x for x in r.json().get('stockList',[]) if x.get('orgId')}
        company = self._directory.get(symbol[2:])
        if not company:
            raise ValueError('官方公告目录暂未识别该证券；可以尝试公司名称网页检索')
        query = str(query or '').strip()[:80]
        r = await self.client.post('https://www.cninfo.com.cn/new/hisAnnouncement/query',
            headers={'Referer':'https://www.cninfo.com.cn/'},data={
                'stock':symbol[2:]+','+company['orgId'],'tabName':'fulltext','pageSize':'12','pageNum':'1',
                'column':{'sh':'sse','sz':'szse','bj':'bse'}[symbol[:2]],'searchkey':query,
                'category':'' if query else 'category_ndbg_szsh;category_bndbg_szsh;category_sjdbg_szsh',
                'seDate':(now().date()-timedelta(days=1100)).isoformat()+'~'+now().date().isoformat(),
                'sortName':'time','sortType':'desc','isHLtitle':'true'})
        r.raise_for_status()
        payload = r.json()
        sources = []
        for row in payload.get('announcements') or []:
            if row.get('secCode') and row['secCode']!=symbol[2:]:
                continue
            path = row.get('adjunctUrl') or ''
            if not path.lower().endswith('.pdf') or not path.startswith('finalpage/'):
                continue
            url = 'https://static.cninfo.com.cn/'+path
            published = None
            raw_time = row.get('announcementTime')
            if isinstance(raw_time,(int,float)):
                published = datetime.fromtimestamp(raw_time/1000,tz=CST).date().isoformat()
            path_date = re.search(r'/([0-9]{4}-[0-9]{2}-[0-9]{2})/',url)
            admissible = bool(published and path_date and path_date.group(1)==published)
            title = BeautifulSoup(row.get('announcementTitle','公告'),'html.parser').get_text('',strip=True)
            sources.append(dict(id=hashlib.sha256(url.encode()).hexdigest()[:16],title=title,url=url,
                company=symbol,company_name=company.get('zwjc'),topic='公司公告 / 定期报告',
                published_at=published,fetched_at=timestamp(),readable=True,source_kind='official_filing',
                point_in_time_admissible=admissible,publication_basis='cninfo_announcement' if admissible else '',
                snippet=f"{company.get('zwjc','')} · 官方公告 · {published or '日期未知'}",
                metadata=dict(announcement_id=row.get('announcementId'),publisher='巨潮资讯',
                    original_publication_time=raw_time,official_fixed_document=admissible)))
        self.observed('search_company_filings')
        return self.register(dict(symbol=symbol,company=company.get('zwjc'),query=query,sources=sources,
            total=payload.get('totalAnnouncement',len(sources)),
            note='官方定期报告/公告索引；阅读 PDF 后正文归档。公告日来自官方元数据，报告期不等于发布日期。'))

    @observe_failures('search_industry_reports')
    async def search_industry_reports(self, query):
        query = str(query or '').strip()[:120]
        if not query:
            raise ValueError('请提供行业、产业链或研究主题')
        sources, warnings = [], []
        try:
            if self._report_cache is None or self._report_cache[0]!=now().date().isoformat():
                rows = []
                for page in (1,2):
                    r = await self.client.get('https://reportapi.eastmoney.com/report/list',
                        params={'industryCode':'*','pageSize':100,'industryType':1,'pageNo':page,'qType':1,
                            'beginTime':(now().date()-timedelta(days=90)).isoformat(),'endTime':now().date().isoformat()},
                        headers={'Referer':'https://data.eastmoney.com/'})
                    r.raise_for_status()
                    rows.extend(r.json().get('data') or [])
                self._report_cache = (now().date().isoformat(),rows)
            terms = [t for t in query_terms(query) if t not in ('行业','报告','研究','研报','分析')]
            ranked = []
            for row in self._report_cache[1]:
                hay = (str(row.get('title',''))+' '+str(row.get('industryName',''))).lower()
                score = sum(len(t) for t in terms if t in hay)
                if score:
                    ranked.append((score,row))
            for _,row in sorted(ranked,key=lambda x:x[0],reverse=True)[:6]:
                code = str(row.get('infoCode',''))
                if not re.fullmatch(r'AP[0-9]+',code):
                    continue
                url = f'https://pdf.dfcfw.com/pdf/H3_{code}_1.pdf'
                sources.append(dict(id=hashlib.sha256(url.encode()).hexdigest()[:16],title=row.get('title','行业报告'),
                    url=url,snippet=f"{row.get('orgSName','')} · {row.get('industryName','')} · {row.get('publishDate','')[:10]}",
                    published_at=publication_date(row.get('publishDate')),fetched_at=timestamp(),readable=True,
                    source_kind='industry_report',topic=query,point_in_time_admissible=False,
                    publication_basis='third_party_report_index',metadata=dict(publisher=row.get('orgSName'),
                        industry=row.get('industryName'),info_code=code),
                    note='第三方研报索引；PDF 可能需要登录/触发反爬，未知修订记录，默认不进入历史回放'))
        except Exception as exc:
            warnings.append('研报目录获取失败：'+str(exc)[:180])
        try:
            # The official release index is a readable primary-source fallback.
            # This is title matching, not a claim of exhaustive industry coverage.
            if self._statistics_cache is None or self._statistics_cache[0]!=now().date().isoformat():
                index_url = 'https://www.stats.gov.cn/sj/zxfb/'
                response = await self.client.get(index_url)
                response.raise_for_status()
                soup = BeautifulSoup(response.content,'html.parser')
                releases = {}
                for link in soup.select('a[href]'):
                    url = urljoin(index_url,link['href'])
                    title = link.get_text(' ',strip=True)
                    date_match = re.search(r'/t([0-9]{8})_',url)
                    if not date_match or not title or not url.startswith(index_url):
                        continue
                    if url not in releases or len(title)>len(releases[url]['title']):
                        date_string = date_match.group(1)
                        releases[url] = dict(title=title,url=url,
                            published_at=f'{date_string[:4]}-{date_string[4:6]}-{date_string[6:]}')
                self._statistics_cache = (now().date().isoformat(),list(releases.values()))
            words = [t for t in query_terms(query) if t not in ('行业','报告','研究','研报','分析')]
            for row in self._statistics_cache[1]:
                if not any(t in row['title'].lower() for t in words):
                    continue
                url = row['url']
                sources.append(dict(id=hashlib.sha256(url.encode()).hexdigest()[:16],**row,
                    snippet='国家统计局公开发布 · '+row['published_at'],fetched_at=timestamp(),readable=True,
                    source_kind='industry_publication',topic=query,point_in_time_admissible=False,
                    publication_basis='official_release_url_date',
                    note='国家统计局当前发布页；日期来自发布路径，网页可能修订，默认不作为历史时点证据'))
                if sum(s['source_kind']=='industry_publication' for s in sources)>=3:
                    break
        except Exception as exc:
            warnings.append('官方统计发布目录获取失败：'+str(exc)[:180])
        try:
            web = await self.search(query+' 行业 数据 报告 (site:stats.gov.cn OR site:miit.gov.cn OR site:gov.cn)')
            for source in web['sources']:
                host = urlparse(source['url']).hostname or ''
                if host.endswith('.gov.cn') or host=='gov.cn':
                    source.update(topic=query,source_kind='industry_publication')
                    sources.append(source)
        except Exception as exc:
            warnings.append('官方行业网站搜索失败：'+str(exc)[:180])
        if not sources and warnings:
            raise ValueError('；'.join(warnings))
        self.observed('search_industry_reports')
        return self.register(dict(query=query,sources=sources[:10],warnings=warnings,
            note='券商目录仅检索最近 90 天内最新 200 份研报，并按标题匹配国家统计局最新发布、补充政府网站搜索；并非完整行业数据库。空结果表示本次未找到匹配资料。'))

    @observe_failures('company_financials')
    async def financials(self, symbol):
        symbol = normalize_symbol(symbol)
        url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
        r = await self.client.get(url,params={'reportName':'RPT_DMSK_FN_INCOME',
            'columns':'SECUCODE,SECURITY_NAME_ABBR,REPORT_DATE,NOTICE_DATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT',
            'filter':f'(SECURITY_CODE="{symbol[2:]}")','pageSize':4,'sortColumns':'REPORT_DATE','sortTypes':'-1'})
        r.raise_for_status()
        records = (r.json().get('result') or {}).get('data') or []
        if not records:
            raise ValueError('该证券当前没有可读取的公开财务报表；请检索官方公告')
        rows = [dict(report_period=publication_date(row.get('REPORT_DATE')),
            published_at=publication_date(row.get('NOTICE_DATE')),
            revenue_cny=row.get('TOTAL_OPERATE_INCOME'),parent_net_profit_cny=row.get('PARENT_NETPROFIT'))
            for row in records]
        title = str(records[0].get('SECURITY_NAME_ABBR') or symbol)+' · 公开利润表摘要'
        document = self.evidence.add(title=title,text=json.dumps(rows,ensure_ascii=False,indent=2),
            url=str(r.url),company=symbol,topic='营业收入 / 归母净利润',source_kind='financial_dataset',
            point_in_time_admissible=False,publication_basis='第三方当前快照，可能包含历史更正数据',
            metadata={'provider':'东方财富','currency':'CNY','report_periods':[x['report_period'] for x in rows]})
        self.observed('company_financials')
        return dict(symbol=symbol,company=records[0].get('SECURITY_NAME_ABBR'),rows=rows,
            money_unit='人民币元 CNY',source=str(r.url),fetched_at=document['fetched_at'],
            document_id=document['id'],chunks=document['chunks'][:4],
            note='第三方公开报表摘要，按报告期累计口径；季度、半年、年度不可直接当单季互比。原始公告优先。当前数据可能经修订，禁止用于历史回放。')

    async def import_document(self, *, title, text=None, pdf=None, url='', published_at=None,
                              company='', topic='', point_in_time_admissible=False, publication_basis=''):
        if bool(text) == bool(pdf):
            raise ValueError('请提供文本或 PDF 文件中的一种')
        metadata = {}
        if pdf is not None:
            if len(pdf)>MAX_DOWNLOAD:
                raise ValueError('PDF 文件不能超过 12 MB')
            text, metadata = await asyncio.to_thread(extract_pdf,pdf)
        return self.evidence.add(title=title,text=text,url=url or '',published_at=published_at,company=company,
            topic=topic,source_kind='user_pdf' if pdf is not None else 'user_text',
            point_in_time_admissible=point_in_time_admissible,publication_basis=publication_basis,
            metadata={**metadata,'admission':'用户声明，系统未独立认证' if point_in_time_admissible else '仅当前研究'})

    @observe_failures('read_source')
    async def read(self, source):
        url = source['url']
        if not allowed_url(url):
            raise ValueError('该来源不在可抓取财经网站、政府网站与公司官网列表；可上传有权使用的原始文件')
        cache_key = (url,source.get('published_at'),bool(source.get('point_in_time_admissible')))
        if cache_key in self.cache:
            source['document_id'] = self.cache[cache_key]['document_id']
            return self.cache[cache_key]
        async with self.sem:
            for _ in range(4):
                if not allowed_url(url):
                    raise ValueError('重定向目标不在允许范围')
                addresses = await asyncio.to_thread(socket.getaddrinfo,urlparse(url).hostname,443)
                if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                    raise ValueError('禁止访问内部网络地址')
                async with self.client.stream('GET',url) as response:
                    if response.is_redirect:
                        url = urljoin(url,response.headers.get('location',''))
                        continue
                    response.raise_for_status()
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload)>MAX_DOWNLOAD:
                            raise ValueError('文档超过 12 MB，请改读摘要或拆分后上传')
                    kind = response.headers.get('content-type','')
                    encoding = response.encoding or 'utf-8'
                published = source.get('published_at')
                if 'pdf' in kind or urlparse(url).path.lower().endswith('.pdf'):
                    text, metadata = await asyncio.to_thread(extract_pdf,payload)
                    note = f"PDF 提取 {metadata['pages_extracted']}/{metadata['pages']} 页；{'已截断，请核对全文' if metadata['truncated'] else '可搜索文本'}"
                else:
                    soup = BeautifulSoup(bytes(payload),'html.parser',from_encoding=encoding)
                    if not published:
                        for attrs in ({'property':'article:published_time'},{'name':'PubDate'},{'name':'publishdate'},{'name':'date'}):
                            meta = soup.find('meta',attrs=attrs)
                            if meta and meta.get('content'):
                                try:
                                    published = publication_date(meta['content'])
                                except ValueError:
                                    continue
                                break
                    for tag in soup(['script','style','nav','header','footer']):
                        tag.decompose()
                    text = soup.get_text(' ',strip=True)[:MAX_TEXT]
                    metadata = dict(format='html',truncated=len(text)>=MAX_TEXT)
                    note = '网页可见正文，可能含模板文本；抓取日期不等于原始发布日期'
                document = self.evidence.add(title=source['title'],text=text,url=url,published_at=published,
                    company=source.get('company',''),topic=source.get('topic',''),
                    source_kind=source.get('source_kind','web_page'),
                    point_in_time_admissible=bool(source.get('point_in_time_admissible')
                        and source.get('source_kind')=='official_filing' and metadata.get('format')=='pdf'
                        and url==source['url'] and urlparse(url).hostname=='static.cninfo.com.cn'),
                    publication_basis=source.get('publication_basis',''),
                    metadata={**source.get('metadata',{}),**metadata,'extraction_note':note})
                result = dict(title=document['title'],url=url,text=text[:6000],fetched_at=document['fetched_at'],
                    published_at=document['published_at'],document_id=document['id'],source_hash=document['source_hash'],
                    point_in_time_admissible=document['point_in_time_admissible'],chunks=document['chunks'][:6],
                    chunk_count=document['chunk_count'],note=note+'；更多分块可调用 search_evidence/read_evidence')
                source['document_id'] = document['id']
                self.cache[cache_key] = result
                self.observed('read_source')
                return result
        raise ValueError('重定向次数过多')
