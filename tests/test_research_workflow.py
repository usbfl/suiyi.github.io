import copy
import json

import pytest


async def test_agent_research_notes_keep_canonical_citations_and_freeze_in_validation(arena, dataset, monkeypatch):
    day = dataset['dates'][30]
    doc = arena.research.evidence.add(title='Fixture annual report',
        text='Verified original report text about revenue and cash flow. This is synthetic evidence for tests only.',
        url='https://static.cninfo.com.cn/finalpage/fixture.pdf', published_at=day,
        point_in_time_admissible=True, publication_basis='user supplied test fixture')
    citation = dict(document_id=doc['id'], chunk_id=doc['chunks'][0]['id'],
        quote='original report text about revenue and cash flow')
    async def policy(player, context, execute, emit):
        search = await execute('search_evidence', {'query': 'revenue'})
        assert {d['id'] for d in search['documents']} == {doc['id']}
        if context['learning_phase'] == 'training':
            result = await execute('save_research_note', {'note': 'Check the revenue claim against its source.', 'citations': [citation]})
            await emit('save_research_note', 'Cited research', {'citations': [citation]}, result)
        else:
            with pytest.raises(ValueError, match='冻结'):
                await execute('save_research_note', {'note': 'Validation cannot rewrite memory.', 'citations': [citation]})
        return (await execute('finish', dict(summary='done', risk='risk', lesson='lesson', next_strategy='evidence based policy')))['summary']
    monkeypatch.setattr(arena.agents, 'run', policy)
    await arena.new('replay', day, models=['fixture-model'], end=dataset['dates'][31], validation_start=dataset['dates'][31])
    await arena.step('citation-training-round')
    first = copy.deepcopy(arena.state['players'][0]['notes'])
    assert first[0]['kind'] == 'research'
    assert first[0]['citations'][0]['quote'] == citation['quote']
    assert first[0]['citations'][0]['source_hash'] == doc['source_hash']
    await arena.step('citation-validation-round')
    assert arena.state['players'][0]['notes'] == first
    assert arena.state['players'][0]['checkpoint']['notes'] == first
    row = arena.db.execute("SELECT result FROM traces WHERE tool='save_research_note'").fetchone()
    assert json.loads(row[0])['citations'][0]['document_id'] == doc['id']
    await arena.promote([arena.state['players'][0]['id']])
    assert arena.state['players'][0]['notes'] == first


async def test_historical_agent_cannot_force_network_research_or_direct_future_reads(arena, dataset):
    await arena.new('replay', dataset['dates'][30], models=['fixture-model'])
    p = arena.state['players'][0]
    doc = arena.research.evidence.add(title='Future report', text='This report becomes public after the current replay date.',
        published_at=dataset['dates'][31], point_in_time_admissible=True, publication_basis='test fixture')
    for name, args in (
        ('search_company_filings', {'symbol': 'sh600519'}),
        ('search_industry_reports', {'query': 'energy'}),
        ('company_financials', {'symbol': 'sh600519'}),
        ('read_evidence', {'document_id': doc['id']}),
    ):
        with pytest.raises(ValueError):
            await arena.execute(arena.state, p, {}, name, args)
    result = await arena.execute(arena.state, p, {}, 'search_evidence', {'query': 'report'})
    assert result['documents'] == []
    assert p['notes'] == [] and p['orders'] == []


async def test_source_id_citation_requires_the_source_to_be_read_first(arena):
    p = arena.state['players'][0]
    source = dict(id='just-a-search-hit', title='Index only', url='https://www.byd.com/report.html')
    with pytest.raises(ValueError, match='归档|读取'):
        await arena.execute(arena.state, p, {source['id']: source}, 'save_research_note',
            {'note': 'A title alone does not prove this conclusion.',
             'citations': [{'source_id': source['id'], 'quote': 'Title was never read in the source.'}]})
    assert p['notes'] == []
