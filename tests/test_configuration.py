"""Configuration must affect execution while credentials stay outside experiment data."""
import copy
import json
from datetime import timedelta

import httpx
import pytest


def connection(arena, suffix='a', **overrides):
    data = dict(name='Test provider ' + suffix, adapter='openai_compatible',
        base_url='https://provider-' + suffix + '.test/v1', models=['custom-model-' + suffix],
        api_key='ONLY_SYNTHETIC_SECRET_' + suffix + '_987654321')
    data.update(overrides)
    return arena.configuration.save_connection(data, creating=True)


def profile(arena, conn, suffix='a', **overrides):
    data = dict(name='Agent ' + suffix, style='Use verified observations only.',
        connection_id=conn['id'], model='custom-model-' + suffix, temperature=.12,
        max_tokens=444, max_steps=2, tools=['price_history'])
    data.update(overrides)
    return arena.configuration.save_profile(data)


def finish_response(summary='mock review'):
    args = dict(summary=summary, risk='mock risk', lesson='mock lesson', next_strategy='mock policy')
    return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [
        {'id': 'finish-1', 'type': 'function', 'function': {'name': 'finish', 'arguments': json.dumps(args)}}
    ]}}], 'usage': {'total_tokens': 10}})


async def test_connection_secrets_are_encrypted_redacted_and_retained_on_blank_update(arena):
    c = connection(arena)
    secret = 'ONLY_SYNTHETIC_SECRET_a_987654321'
    assert c['has_key'] and 'api_key' not in c
    row = arena.db.execute('SELECT payload,secret FROM connections WHERE id=?', (c['id'],)).fetchone()
    assert row[1].startswith('fernet:') and secret not in ''.join(row)
    public = arena.configuration.connection(c['id'])
    assert 'api_key' not in public
    assert arena.configuration.connection(c['id'], secret=True)['api_key'] == secret
    arena.configuration.save_connection({'name': 'Renamed', 'api_key': '   '}, ident=c['id'])
    assert arena.configuration.connection(c['id'], secret=True)['api_key'] == secret
    assert secret not in '\n'.join(arena.db.iterdump())
    assert secret not in json.dumps(arena.configuration.connections())
    arena.configuration.save_connection({'clear_key': True}, ident=c['id'])
    assert not arena.configuration.connection(c['id'])['has_key']
    assert arena.configuration.connection(c['id'], secret=True)['api_key'] == ''


async def test_explicit_default_key_clear_does_not_resurrect_legacy_env_key(arena):
    assert arena.configuration.connection('default')['has_key']
    arena.configuration.save_connection({'clear_key': True}, ident='default')
    before = copy.deepcopy(arena.state)
    with pytest.raises(ValueError, match='密钥|连接'):
        await arena.step('cleared-key-no-fallback')
    assert arena.state == before and arena.agents.calls == 0


async def test_profile_and_execution_settings_are_snapshotted_per_experiment(arena):
    c = connection(arena)
    p = profile(arena, c)
    arena.configuration.save_settings({'max_position_pct': 10, 'slippage_bps': 40,
        'decision_interval_seconds': 120, 'min_decision_gap_seconds': 20})
    await arena.new('live', profile_ids=[p['id']])
    before = copy.deepcopy(arena.state)
    arena.configuration.save_profile({'temperature': .9, 'max_steps': 4, 'model': 'changed-model'}, ident=p['id'])
    arena.configuration.save_settings({'max_position_pct': 80, 'slippage_bps': 0})
    assert arena.state == before
    assert arena.state['players'][0]['temperature'] == .12
    assert arena.state['players'][0]['max_steps'] == 2
    assert arena.state['runtime_settings']['max_position_pct'] == 10
    await arena.new('live', profile_ids=[p['id']])
    assert arena.state['players'][0]['model'] == 'changed-model'
    assert arena.state['players'][0]['temperature'] == .9
    assert arena.state['runtime_settings']['max_position_pct'] == 80
    arena.configuration.delete_profile(p['id'])
    assert arena.state['players'][0]['model'] == 'changed-model'
    with pytest.raises(ValueError):
        arena.configuration.delete_connection(c['id'])


async def test_concurrent_agents_route_to_distinct_provider_credentials_and_parameters(arena):
    a = connection(arena, 'a')
    b = connection(arena, 'b')
    pa = profile(arena, a, 'a')
    pb = profile(arena, b, 'b', temperature=.88, max_tokens=555, tools=['market_scan'])
    await arena.new('live', profile_ids=[pa['id'], pb['id']])
    requests = []
    def respond(request):
        requests.append((str(request.url), request.headers.get('authorization'), json.loads(request.content)))
        return finish_response()
    await arena.agents.client.aclose()
    arena.agents.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    await arena.step('provider-isolated-round')
    assert len(requests) == 2
    by_model = {body['model']: (url, auth, body) for url, auth, body in requests}
    for suffix, temperature, tokens, permitted in [('a', .12, 444, 'price_history'), ('b', .88, 555, 'market_scan')]:
        url, auth, body = by_model['custom-model-' + suffix]
        assert url == 'https://provider-' + suffix + '.test/v1/chat/completions'
        assert auth == 'Bearer ONLY_SYNTHETIC_SECRET_' + suffix + '_987654321'
        assert body['temperature'] == temperature and body['max_tokens'] == tokens
        assert 'thinking' not in body
        assert {t['function']['name'] for t in body['tools']} == {permitted, 'finish', 'get_account'}
    assert all(len(p['versions']) == 1 for p in arena.state['players'])
    assert 'ONLY_SYNTHETIC_SECRET' not in json.dumps(arena.public())
    assert 'SK_TEST_ONLY_NO_PROVIDER' not in json.dumps(arena.public())


async def test_ungranted_model_tool_is_rejected_and_custom_step_limit_forces_finish(arena):
    c = connection(arena)
    p = profile(arena, c, tools=['price_history'], max_steps=2)
    await arena.new('live', profile_ids=[p['id']])
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            args = dict(symbol='sh600519', side='buy', quantity=100, limit_price=10, reason='unauthorized')
            return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [{
                'id': 'ungranted', 'type': 'function', 'function': {'name': 'place_order', 'arguments': json.dumps(args)}
            }]}}], 'usage': {'total_tokens': 10}})
        assert '权限' in next(m['content'] for m in body['messages'] if m['role'] == 'tool')
        assert body['tool_choice'] == {'type': 'function', 'function': {'name': 'finish'}}
        assert [t['function']['name'] for t in body['tools']] == ['finish']
        return finish_response()
    await arena.agents.client.aclose()
    arena.agents.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    await arena.step('forbidden-tool-round')
    assert len(calls) == 2 and arena.state['players'][0]['orders'] == []
    with pytest.raises(ValueError, match='权限'):
        await arena.execute(arena.state, arena.state['players'][0], {}, 'place_order',
            dict(symbol='sh600519', side='buy', quantity=100, limit_price=10, reason='bypass attempt'))


async def test_connection_edit_cannot_switch_provider_halfway_through_agent_round(arena):
    c = connection(arena)
    p = profile(arena, c)
    await arena.new('live', profile_ids=[p['id']])
    requests = []
    def respond(request):
        requests.append((str(request.url), request.headers.get('authorization')))
        if len(requests) == 1:
            arena.configuration.save_connection({'base_url': 'https://replacement.test/v1',
                'api_key': 'NEW_SYNTHETIC_ROUND_SECRET'}, ident=c['id'])
            return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [{
                'id': 'history', 'type': 'function', 'function': {'name': 'price_history',
                'arguments': json.dumps({'symbol': 'sh600519'})}
            }]}}], 'usage': {'total_tokens': 10}})
        return finish_response()
    await arena.agents.client.aclose()
    arena.agents.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    await arena.step('connection-snapshot-round')
    await arena.step('connection-next-round')
    assert requests[:2] == [('https://provider-a.test/v1/chat/completions',
        'Bearer ONLY_SYNTHETIC_SECRET_a_987654321')] * 2
    assert requests[2] == ('https://replacement.test/v1/chat/completions', 'Bearer NEW_SYNTHETIC_ROUND_SECRET')


async def test_experiment_position_setting_is_enforced_inside_tool_execution(arena):
    arena.configuration.save_settings({'max_position_pct': 1})
    await arena.new('live', models=['freeform-test-model'])
    p = arena.state['players'][0]
    with pytest.raises(ValueError, match='1%'):
        await arena.execute(arena.state, p, {}, 'place_order',
            dict(symbol='sh600519', side='buy', quantity=2000, limit_price=11, reason='risk test'))
    assert p['orders'] == []


@pytest.mark.parametrize('setting,elapsed,move,expected', [
    ({'decision_interval_seconds': 120, 'min_decision_gap_seconds': 20}, 121, 0, True),
    ({'decision_interval_seconds': 120, 'min_decision_gap_seconds': 20}, 100, 0, False),
    ({'decision_interval_seconds': 120, 'min_decision_gap_seconds': 20, 'decision_move_pct': 2}, 30, .021, True),
    ({'decision_interval_seconds': 120, 'min_decision_gap_seconds': 20, 'decision_move_pct': 2}, 30, .01, False),
])
async def test_custom_live_trigger_settings_control_wakeup(arena, monkeypatch, setting, elapsed, move, expected):
    import app.engine as engine
    arena.configuration.save_settings(setting)
    await arena.new('live', models=['freeform-test-model'])
    current = engine.now()
    arena.state['last_decision_at'] = (current-timedelta(seconds=elapsed)).isoformat()
    arena.state['decision_prices'] = {s: q['price']/(1+move) for s, q in arena.state['quotes'].items()}
    monkeypatch.setattr(engine, 'now', lambda: current)
    monkeypatch.setattr(engine, 'market_open', lambda q: True)
    calls = []
    async def step(ident):
        calls.append(ident)
    async def sleep(seconds):
        arena.running = False
    monkeypatch.setattr(arena, 'step', step)
    monkeypatch.setattr(engine.asyncio, 'sleep', sleep)
    arena.running = True
    await arena.auto()
    assert bool(calls) == expected
