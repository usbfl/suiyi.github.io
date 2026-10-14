"""Source provenance, historical time boundaries and verbatim citation checks."""
import sqlite3

import pytest

from app.evidence import EvidenceStore


@pytest.fixture
def evidence(tmp_path):
    db = sqlite3.connect(tmp_path / 'evidence.sqlite3')
    store = EvidenceStore(db)
    yield store
    db.close()


def document(store, *, published_at='2026-08-01', admitted=True, title='历史行业证据', text=None):
    return store.add(title=title,
        text=text or '用于测试的连续原文：公司报告收入增长，现金流数据仍需要结合原始附注核对。',
        url='https://static.cninfo.com.cn/finalpage/test.pdf',
        published_at=published_at, company='测试公司', topic='新能源',
        source_kind='filing', point_in_time_admissible=admitted,
        publication_basis='cninfo_announcement' if admitted else '')


def test_historical_evidence_search_excludes_future_unknown_and_unadmitted(evidence):
    good = document(evidence)
    document(evidence, published_at='2026-09-01', title='未来报告')
    document(evidence, published_at=None, title='未知发布日期')
    document(evidence, admitted=False, title='发布日期未经确认')
    found = evidence.search(as_of='2026-08-15')
    assert {d['id'] for d in found['documents']} == {good['id']}
    assert evidence.get(good['id'], as_of='2026-08-15')['id'] == good['id']


@pytest.mark.parametrize('published_at,admitted', [
    ('2026-08-16', True), (None, True), ('2026-08-01', False),
])
def test_read_and_citation_cannot_bypass_historical_cutoff(evidence, published_at, admitted):
    doc = document(evidence, published_at=published_at, admitted=admitted)
    with pytest.raises(ValueError):
        evidence.get(doc['id'], as_of='2026-08-15')
    chunk = doc['chunks'][0]
    with pytest.raises(ValueError):
        evidence.validate_citations([{'document_id': doc['id'], 'chunk_id': chunk['id'],
            'quote': chunk['text'][:12]}], as_of='2026-08-15')


def test_citations_require_matching_document_chunk_and_contiguous_quote(evidence):
    doc = document(evidence)
    chunk = doc['chunks'][0]
    quote = chunk['text'][3:24]
    citations = evidence.validate_citations([{'document_id': doc['id'], 'chunk_id': chunk['id'],
        'quote': quote}], as_of='2026-08-15')
    assert len(citations) == 1
    assert citations[0]['document_id'] == doc['id']
    assert citations[0]['chunk_id'] == chunk['id']
    assert citations[0]['quote'] == quote
    for changed in (
        {'document_id': doc['id'], 'chunk_id': 'unknown:0001', 'quote': quote},
        {'document_id': doc['id'], 'chunk_id': chunk['id'], 'quote': '模型凭空补写的结论'},
    ):
        with pytest.raises(ValueError):
            evidence.validate_citations([changed], as_of='2026-08-15')


def test_chunk_selection_is_scoped_to_its_document(evidence):
    a = document(evidence, title='报告 A', text='A 的数据来源原文。' * 120)
    b = document(evidence, title='报告 B', text='B 的数据来源原文。' * 120)
    selected = evidence.get(a['id'], as_of='2026-08-15', chunk_ids=[a['chunks'][0]['id']])
    assert [c['id'] for c in selected['chunks']] == [a['chunks'][0]['id']]
    with pytest.raises(ValueError):
        evidence.get(a['id'], as_of='2026-08-15', chunk_ids=[b['chunks'][0]['id']])
