"""Content-addressed research archive with explicit point-in-time admission.

An old publication date does not establish that today's web content existed then.
Only fixed official filings or an explicit user-supplied historical attestation are
admitted to replay. Search, read and citations all apply the same boundary.
"""
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime
from urllib.parse import urlparse
from .config import timestamp

MAX_TEXT = 180_000


def publication_date(value):
    if value is None or str(value).strip() == '':
        return None
    raw = str(value).strip()
    try:
        if len(raw)>10 and raw[10] not in ('T',' '):
            raise ValueError('invalid date suffix')
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError as exc:
        raise ValueError('发布日期必须为 YYYY-MM-DD；未知日期请留空') from exc


def query_terms(query):
    query = str(query or '').strip().lower()[:200]
    terms = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', query)
    expanded = []
    for term in terms:
        expanded.append(term)
        if re.fullmatch(r'[\u4e00-\u9fff]{3,}', term):
            expanded.extend(term[i:i+2] for i in range(len(term)-1))
    return list(dict.fromkeys(expanded))[:40]


class EvidenceStore:
    def __init__(self, db=None):
        self.db = db if db is not None else sqlite3.connect(':memory:')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS evidence_documents(
                id TEXT PRIMARY KEY,title TEXT NOT NULL,url TEXT NOT NULL,published_at TEXT,
                fetched_at TEXT NOT NULL,company TEXT NOT NULL,topic TEXT NOT NULL,
                source_kind TEXT NOT NULL,source_hash TEXT NOT NULL,content_hash TEXT NOT NULL,
                point_in_time_admissible INTEGER NOT NULL,publication_basis TEXT NOT NULL,
                metadata TEXT NOT NULL,characters INTEGER NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence_chunks(
                id TEXT PRIMARY KEY,document_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
                text TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,
                FOREIGN KEY(document_id) REFERENCES evidence_documents(id));
            CREATE INDEX IF NOT EXISTS evidence_chunk_document ON evidence_chunks(document_id,ordinal);
            CREATE INDEX IF NOT EXISTS evidence_publication ON evidence_documents(published_at);
        ''')

    @staticmethod
    def _metadata(row):
        keys = ('id','title','url','published_at','fetched_at','company','topic','source_kind',
                'source_hash','content_hash','point_in_time_admissible','publication_basis',
                'metadata','characters','created_at')
        result = dict(zip(keys, row))
        result['point_in_time_admissible'] = bool(result['point_in_time_admissible'])
        result['metadata'] = json.loads(result['metadata'])
        result['historical_eligible'] = bool(result['point_in_time_admissible'] and result['published_at'])
        return result

    @staticmethod
    def _admit(document, as_of):
        if as_of is None:
            return True
        cutoff = publication_date(as_of)
        if not cutoff:
            raise ValueError('历史查询必须提供明确的回溯日期')
        return bool(document['point_in_time_admissible'] and document['published_at']
                    and document['published_at'] <= cutoff)

    def add(self, *, title, text, url='', published_at=None, company='', topic='',
            source_kind='user_text', point_in_time_admissible=False, publication_basis='',
            fetched_at=None, metadata=None):
        title = str(title or '').strip()[:240]
        content = str(text or '').replace('\x00','').strip()
        if not title:
            raise ValueError('资料标题不能为空')
        if len(content) < 20:
            raise ValueError('有效正文少于 20 个字符；扫描 PDF 需要先转换为可搜索文本')
        if len(content) > MAX_TEXT:
            raise ValueError(f'正文最多 {MAX_TEXT} 字符，请拆分资料')
        published = publication_date(published_at)
        basis = str(publication_basis or '').strip()[:1000]
        if point_in_time_admissible and not basis:
            raise ValueError('历史可用资料必须注明时点依据，例如官方公告日期或原始历史文件来源')
        url = str(url or '')[:2000]
        if url:
            parsed = urlparse(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('来源链接必须是无账号密码的 HTTP(S) 网址；上传文件可留空')
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        source_hash = hashlib.sha256((url+'\n'+content).encode()).hexdigest()
        identity = json.dumps([source_hash,published,bool(point_in_time_admissible),basis],ensure_ascii=False)
        ident = hashlib.sha256(identity.encode()).hexdigest()[:24]
        existing = self.db.execute('SELECT id FROM evidence_documents WHERE id=?',(ident,)).fetchone()
        if existing:
            return self.get(ident)
        fetched = str(fetched_at or timestamp())
        info = json.dumps(metadata or {},ensure_ascii=False)
        chunks = []
        start = 0
        while start < len(content):
            end = min(start+1200,len(content))
            if end < len(content):
                boundary = max(content.rfind('\n',start+700,end),content.rfind('。',start+700,end))
                if boundary > start+700:
                    end = boundary+1
            ordinal = len(chunks)+1
            chunks.append((f'{ident}:{ordinal:04d}',ident,ordinal,content[start:end],start,end))
            if end >= len(content):
                break
            start = end-120
        with self.db:
            self.db.execute('INSERT INTO evidence_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident,title,url,published,fetched,str(company or '')[:120],str(topic or '')[:180],
                 str(source_kind)[:50],source_hash,content_hash,int(bool(point_in_time_admissible)),
                 basis,info,len(content),timestamp()))
            self.db.executemany('INSERT INTO evidence_chunks VALUES(?,?,?,?,?,?)',chunks)
        return self.get(ident)

    def get(self, document_id, *, as_of=None, chunk_ids=None):
        row = self.db.execute('SELECT * FROM evidence_documents WHERE id=?',(str(document_id),)).fetchone()
        if not row:
            raise ValueError('资料不存在，请先搜索或读取来源并归档')
        result = self._metadata(row)
        if not self._admit(result,as_of):
            raise ValueError('该资料不符合历史时点边界：日期未知、晚于回溯日，或未明确声明历史可用')
        rows = self.db.execute('SELECT * FROM evidence_chunks WHERE document_id=? ORDER BY ordinal',(str(document_id),)).fetchall()
        chunks = [dict(zip(('id','document_id','ordinal','text','start','end'),r)) for r in rows]
        result['chunk_count'] = len(chunks)
        if chunk_ids is not None:
            if not isinstance(chunk_ids,list) or len(chunk_ids)>12:
                raise ValueError('每次最多读取 12 个分块')
            wanted = set(map(str,chunk_ids))
            if not wanted.issubset({c['id'] for c in chunks}):
                raise ValueError('分块不属于这份资料')
            chunks = [c for c in chunks if c['id'] in wanted]
        result['chunks'] = chunks
        return result

    def search(self, query='', *, company=None, as_of=None, limit=8):
        limit = max(1,min(50,int(limit)))
        if as_of is not None:
            if not publication_date(as_of):
                raise ValueError('历史查询必须提供明确的回溯日期')
        terms = query_terms(query)
        found = []
        # The local research archive is intentionally a transparent keyword index,
        # not an embedding service. Chinese bigrams also work with no tokenizer dependency.
        sql = 'SELECT * FROM evidence_documents'
        params = []
        if as_of is not None:
            sql += ' WHERE point_in_time_admissible=1 AND published_at IS NOT NULL AND published_at<=?'
            params.append(publication_date(as_of))
        sql += ' ORDER BY fetched_at DESC LIMIT 1000'
        for row in self.db.execute(sql,params).fetchall():
            doc = self._metadata(row)
            if company and str(company).lower() not in (doc['company']+' '+doc['title']).lower():
                continue
            rows = self.db.execute('SELECT * FROM evidence_chunks WHERE document_id=? ORDER BY ordinal',(doc['id'],)).fetchall()
            chunks = [dict(zip(('id','document_id','ordinal','text','start','end'),r)) for r in rows]
            base = (doc['title']+' '+doc['topic']+' '+doc['company']).lower()
            weighted = []
            for chunk in chunks:
                score = sum((5 if term in base else 0)+min(4,chunk['text'].lower().count(term)) for term in terms)
                if not terms or score:
                    weighted.append((score,chunk))
            if terms and not weighted:
                continue
            weighted.sort(key=lambda item:item[0],reverse=True)
            doc['chunks'] = [item[1] for item in weighted[:3]]
            doc['chunk_count'] = len(chunks)
            doc['score'] = weighted[0][0] if weighted else 0
            found.append(doc)
        found.sort(key=lambda d:(d['score'],d['published_at'] or '',d['fetched_at']),reverse=True)
        return dict(documents=found[:limit],count=len(found),query=str(query or ''),as_of=as_of,
                    retrieval='关键词与中文双字匹配；最多检索最近归档的 1000 份资料',
                    boundary='仅已知发布日期且明确声明历史可用的资料' if as_of else '当前归档资料，历史可用性单独标注')

    def validate_citations(self, citations, *, as_of=None):
        if not isinstance(citations,list) or not 1<=len(citations)<=8:
            raise ValueError('研究笔记需要 1 至 8 条可核对的原文引用')
        canonical = []
        for citation in citations:
            if not isinstance(citation,dict):
                raise ValueError('引用格式无效')
            document_id = citation.get('document_id')
            chunk_id = citation.get('chunk_id')
            quote = str(citation.get('quote') or '').strip()
            if not document_id or not chunk_id or not 6<=len(quote)<=800:
                raise ValueError('引用需提供 document_id、chunk_id 和 6 至 800 字符的连续原文')
            document = self.get(document_id,as_of=as_of,chunk_ids=[chunk_id])
            if quote not in document['chunks'][0]['text']:
                raise ValueError('引用不是所选资料分块的连续原文，请勿改写或拼接引文')
            canonical.append(dict(document_id=document['id'],chunk_id=chunk_id,quote=quote,
                title=document['title'],url=document['url'],published_at=document['published_at'],
                fetched_at=document['fetched_at'],source_hash=document['source_hash'],
                point_in_time_admissible=document['point_in_time_admissible']))
        return canonical
