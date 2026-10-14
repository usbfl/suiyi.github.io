import httpx
import pytest
from datetime import datetime
from urllib.parse import parse_qs
from app.config import CST
from app.research import Research


async def test_regional_search_redirect_retains_source_metadata():
    def transport(request):
        if request.url.host=='www.bing.com':
            return httpx.Response(302,headers={'Location':'https://cn.bing.com/search?q=BYD&format=rss'})
        return httpx.Response(200,text='<rss><channel><item><title>BYD</title><link>https://www.byd.com/cn/index.html</link><description>Company</description></item></channel></rss>')
    r=Research()
    await r.client.aclose()
    r.client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        result=await r.search('BYD')
        source=result['sources'][0]
        assert source['readable'] and source['id'] and source['fetched_at']
        assert source['url']=='https://www.byd.com/cn/index.html'
    finally:await r.client.aclose()


async def test_search_rejects_foreign_redirect():
    r=Research()
    await r.client.aclose()
    r.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(302,headers={'Location':'http://127.0.0.1/'})))
    try:
        with pytest.raises(ValueError,match='重定向'):await r.search('test')
    finally:await r.client.aclose()


async def test_official_filing_uses_publication_date_and_fixed_document_consistency():
    publication = int(datetime(2026,8,1,tzinfo=CST).timestamp()*1000)
    def transport(request):
        if request.url.path.endswith('szse_stock.json'):
            return httpx.Response(200, json={'stockList': [{'code': '600519', 'orgId': 'gssh0600519', 'zwjc': '测试公司'}]})
        assert request.method == 'POST'
        body = parse_qs(request.content.decode())
        assert body['stock'] == ['600519,gssh0600519'] and body['column'] == ['sse']
        return httpx.Response(200, json={'announcements': [
            {'announcementTitle': '2025 年度报告', 'announcementTime': publication,
             'adjunctUrl': 'finalpage/2026-08-01/fixed.pdf'},
            {'announcementTitle': '目录日期冲突', 'announcementTime': publication,
             'adjunctUrl': 'finalpage/2026-08-02/mismatched.pdf'},
        ]})
    research = Research()
    await research.client.aclose()
    research.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        sources = (await research.search_company_filings('sh600519'))['sources']
        assert sources[0]['published_at'] == '2026-08-01'  # Report year is not publication date.
        assert sources[0]['point_in_time_admissible'] is True
        assert sources[0]['publication_basis'] == 'cninfo_announcement'
        assert sources[1]['point_in_time_admissible'] is False
        assert research.evidence.search()['count'] == 0  # An index is not read document evidence.
    finally:
        await research.client.aclose()


async def test_ordinary_rss_date_does_not_admit_current_page_to_history():
    xml = '''<rss><channel><item><title>Company report</title>
    <link>https://www.byd.com/report.html</link><pubDate>Fri, 01 Aug 2025 00:00:00 GMT</pubDate>
    <description>Current mutable page</description></item></channel></rss>'''
    research = Research()
    await research.client.aclose()
    research.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=xml)))
    try:
        source = (await research.search('report'))['sources'][0]
        assert source['published_at'] is None
        assert source['point_in_time_admissible'] is False
    finally:
        await research.client.aclose()


async def test_reading_page_archives_original_chunks_but_keeps_historical_admission_closed(monkeypatch):
    import app.research as module
    monkeypatch.setattr(module.socket, 'getaddrinfo', lambda *args: [(2,1,6,'',('93.184.216.34',443))])
    html = '<html><head><meta property="article:published_time" content="2025-08-01"></head><body>' \
        '<p>Original research evidence about company revenue, cash flow and uncertain demand.</p></body></html>'
    research = Research()
    await research.client.aclose()
    research.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
        httpx.Response(200, text=html, headers={'content-type':'text/html'})))
    try:
        source = dict(id='web-source', title='Mutable website report', url='https://www.byd.com/report.html',
            point_in_time_admissible=False, publication_basis='')
        result = await research.read(source)
        assert result['document_id'] == source['document_id']
        assert result['published_at'] == '2025-08-01'
        assert result['chunks'] and result['source_hash']
        assert 'Original research evidence' in result['chunks'][0]['text']
        assert research.evidence.search()['count'] == 1
        with pytest.raises(ValueError):
            research.evidence.get(result['document_id'], as_of='2026-08-15')
    finally:
        await research.client.aclose()
