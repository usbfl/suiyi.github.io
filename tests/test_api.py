"""Exercise public workflow contracts with an isolated Arena, never the running server."""
import httpx
import pytest

from app.main import app


@pytest.fixture
async def client(arena, monkeypatch):
    monkeypatch.setattr(app.state, 'arena', arena, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as c:
        yield c


async def test_public_config_and_custom_replay_creation(client, arena, dataset):
    config = (await client.get('/api/config')).json()
    assert config['max_players'] == 8 and len(config['presets']) == 6
    assert config['initial_capital'] == 1_000_000
    response = await client.post('/api/matches', json={
        'mode': 'replay', 'title': '接口回归实验', 'start': dataset['dates'][30],
        'end': dataset['dates'][33], 'validation_start': dataset['dates'][32],
        'players': [dict(preset='value', name='独立选手', style='只使用已验证证据。', model='deepseek-flash')],
    })
    assert response.status_code == 200
    body = response.json()
    assert body['title'] == '接口回归实验' and len(body['players']) == 1
    assert body['learning']['total_rounds'] == 4
    assert body['learning']['validation_start'] == dataset['dates'][32]
    saved = (await client.get('/api/matches')).json()
    summary = next(s for s in saved if s['id'] == body['id'])
    assert summary['player_count'] == 1 and summary['learning']['stage'] == 'training'
    assert arena.agents.calls == 0


async def test_api_rejects_invalid_configuration_and_unfinished_promotion(client, arena):
    ident = arena.state['id']
    for payload, status in (({'players': []}, 422), ({'players': [{}] * 9}, 422), ({'models': ['']}, 400)):
        response = await client.post('/api/matches', json=payload)
        assert response.status_code == status
        assert arena.state['id'] == ident
    response = await client.post('/api/promote', json={'player_ids': [arena.state['players'][0]['id']]})
    assert response.status_code == 400 and arena.state['id'] == ident
    response = await client.post('/api/budget', json={'additional_calls': True})
    assert response.status_code == 422
    response = await client.post('/api/budget', json={'additional_calls': 20}, headers={'origin': 'https://unrelated.test'})
    assert response.status_code == 403


async def test_api_promotion_and_export_keep_provenance(client, arena, dataset, monkeypatch):
    async def review(player, context, execute, emit):
        return (await execute('finish', dict(summary='fixture review', risk='risk',
            lesson='lesson', next_strategy='recorded experience')))['summary']
    monkeypatch.setattr(arena.agents, 'run', review)
    source = (await client.post('/api/matches', json={'mode': 'replay',
        'start': dataset['dates'][30], 'end': dataset['dates'][30], 'models': ['deepseek-flash']})).json()
    response = await client.post('/api/step', json={'request_id': 'api-training-001'})
    assert response.status_code == 200
    selected = source['players'][0]['id']
    response = await client.post('/api/promote', json={'player_ids': [selected], 'title': '经验迁移 API'})
    assert response.status_code == 200
    live = response.json()
    assert live['mode'] == 'live' and live['source_match_id'] == source['id']
    assert live['players'][0]['equity'] == 100_000_000
    exported = await client.get('/api/export')
    assert 'attachment;' in exported.headers['content-disposition']
    assert exported.json()['state']['players'][0]['provenance']['source_match_id'] == source['id']
    assert 'test-only' not in exported.text
    restored = await client.post('/api/matches/' + source['id'] + '/load')
    assert restored.status_code == 200 and restored.json()['learning']['stage'] == 'complete'


async def test_connection_profile_api_is_redacted_and_profiles_create_snapshots(client, arena):
    secret = 'API_FIXTURE_ONLY_SECRET_123456789'
    response = await client.post('/api/connections', json={'name': 'Test API provider',
        'adapter': 'openai_compatible', 'base_url': 'https://model.test/v1',
        'api_key': secret, 'models': ['arbitrary-future-model']})
    assert response.status_code == 200
    connection = response.json()
    assert connection['has_key'] and 'api_key' not in connection
    response = await client.put('/api/connections/' + connection['id'], json={'name': 'Updated provider', 'api_key': ''})
    assert response.status_code == 200
    assert arena.configuration.connection(connection['id'], secret=True)['api_key'] == secret
    p = (await client.post('/api/profiles', json={'name': 'Custom protocol Agent',
        'style': 'Read only historical sources.', 'connection_id': connection['id'],
        'model': 'arbitrary-future-model', 'temperature': .15, 'max_steps': 3,
        'max_tokens': 768, 'tools': ['price_history']})).json()
    response = await client.post('/api/matches', json={'mode': 'live', 'profile_ids': [p['id']]})
    assert response.status_code == 200
    selected = response.json()['players'][0]
    assert selected['connection_id'] == connection['id']
    assert selected['model'] == 'arbitrary-future-model' and selected['max_steps'] == 3
    for endpoint in ('/api/connections', '/api/profiles', '/api/config', '/api/state', '/api/export'):
        response = await client.get(endpoint)
        assert response.status_code == 200
        assert secret not in response.text
        assert 'SK_TEST_ONLY_NO_PROVIDER_1234567890' not in response.text
    response = await client.delete('/api/connections/' + connection['id'])
    assert response.status_code == 400  # Existing profile/experiment still depends on it.


async def test_connection_probe_redacts_provider_error_body_and_cross_origin_writes(client, arena):
    secret = 'API_PROBE_SECRET_DO_NOT_RETURN_0123'
    c = arena.configuration.save_connection({'name': 'Probe fixture', 'adapter': 'openai_compatible',
        'base_url': 'https://provider.test/v1', 'models': [], 'api_key': secret}, creating=True)
    await arena.agents.client.aclose()
    def respond(request):
        assert request.headers['authorization'] == 'Bearer ' + secret
        return httpx.Response(401, text='provider echoed key: ' + secret)
    arena.agents.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    response = await client.post('/api/connections/' + c['id'] + '/test')
    assert response.status_code == 200 and response.json()['ok'] is False
    assert secret not in response.text
    for method in ('PUT', 'DELETE'):
        response = await client.request(method, '/api/connections/' + c['id'],
            json={'clear_key': True}, headers={'origin': 'https://unrelated.test'})
        assert response.status_code == 403
    assert arena.configuration.connection(c['id'])['has_key']


async def test_malformed_connection_body_cannot_echo_credentials_in_validation_error(client):
    secret = 'MALFORMED_REQUEST_SYNTHETIC_SECRET_0192837465'
    response = await client.post('/api/connections', json=[{'api_key': secret}])
    assert response.status_code == 422
    assert secret not in response.text
    assert 'input' not in response.text


async def test_settings_api_rejects_invalid_values_without_changing_effective_match(client, arena):
    old = arena.state['runtime_settings'].copy()
    for payload in ({'max_position_pct': 0}, {'max_position_pct': True},
        {'min_decision_gap_seconds': 1000, 'decision_interval_seconds': 60}, {'invented': 1}):
        response = await client.post('/api/settings', json=payload)
        assert response.status_code == 400
    response = await client.post('/api/settings', json={'max_position_pct': 12, 'slippage_bps': 25})
    assert response.status_code == 200 and response.json()['max_position_pct'] == 12
    assert arena.state['runtime_settings'] == old
    response = await client.post('/api/matches', json={'mode': 'live', 'models': ['custom-model']})
    assert response.json()['runtime_settings']['max_position_pct'] == 12


async def test_research_api_import_search_and_historical_probes_preserve_agent_accounts(client, arena):
    import copy
    before = copy.deepcopy(arena.state)
    body = dict(title='Imported test source', text='This original report contains verifiable facts and explicit uncertainty.',
        published_at='2026-08-01', company='Fixture company', point_in_time_admissible=True,
        publication_basis='Explicit user test fixture declaration')
    response = await client.post('/api/evidence/import', json=body)
    assert response.status_code == 200
    doc = response.json()
    assert doc['chunks'] and doc['point_in_time_admissible']
    response = await client.post('/api/research/tools/test', json={'tool': 'search_evidence',
        'args': {'query': 'verifiable'}, 'as_of': '2026-08-15'})
    assert response.status_code == 200
    assert response.json()['result']['documents'][0]['id'] == doc['id']
    response = await client.get('/api/evidence/' + doc['id'], params={'as_of': '2026-07-31'})
    assert response.status_code == 400
    response = await client.post('/api/research/tools/test', json={'tool': 'search_company_filings',
        'args': {'symbol': 'sh600519'}, 'as_of': '2026-08-15'})
    assert response.status_code == 400
    response = await client.post('/api/research/tools/test', json={'tool': 'save_research_note',
        'args': {'note': 'Cannot modify Agent via workbench probe'}})
    assert response.status_code == 400
    response = await client.post('/api/research/tools/test', json={'tool': 'place_order',
        'args': {'symbol': 'sh600519'}})
    assert response.status_code == 400
    assert arena.state == before and arena.agents.calls == 0


async def test_research_import_requires_admission_basis_and_valid_pdf(client):
    response = await client.post('/api/evidence/import', json={'title': 'Missing basis',
        'text': 'The date alone is insufficient to establish historical document contents.',
        'published_at': '2026-08-01', 'point_in_time_admissible': True})
    assert response.status_code == 400
    response = await client.post('/api/evidence/import', json={'title': 'Invalid file', 'pdf_base64': 'not base64!'})
    assert response.status_code == 400
    response = await client.post('/api/evidence/import', json={'title': 'Login page is not a report',
        'pdf_base64': 'PGh0bWw+TG9naW4gcmVxdWlyZWQ8L2h0bWw+'})
    assert response.status_code == 400
