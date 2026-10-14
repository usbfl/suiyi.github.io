"""Workflow regressions use fixture prices and a local policy; no paid model calls."""
import asyncio
import copy
import json

import pytest

from app.broker import INITIAL


async def learning_policy(player, context, execute, emit):
    """Trade once and try to revise memory every day, including validation."""
    if not context['account']['holdings'] and not context['account']['pending']:
        await execute('place_order', dict(symbol='sh600519', side='buy', quantity=100,
            limit_price=12, reason='fixture strategy'))
    try:
        await execute('save_note', {'note': 'observed ' + context['date']})
    except ValueError:
        pass
    return (await execute('finish', dict(summary='fixture review', risk='fixture risk',
        lesson='feedback from ' + context['date'], next_strategy='policy at ' + context['date'])))['summary']


@pytest.mark.parametrize('count', [1, 6, 8])
async def test_competition_supports_flexible_independent_roster(arena, count):
    await arena.new('live', models=['deepseek-flash'] * count)
    players = arena.state['players']
    assert len(players) == count
    assert len({p['id'] for p in players}) == count
    assert all(p['cash'] == INITIAL for p in players)
    if count > 1:
        players[0]['notes'].append({'note': 'private'})
        players[0]['orders'].append({'id': 'private'})
        players[0]['cash'] -= 100
        assert players[1]['notes'] == players[1]['orders'] == []
        assert players[1]['cash'] == INITIAL


async def test_default_roster_has_six_and_custom_role_is_preserved(arena):
    await arena.new('live')
    assert len(arena.state['players']) == 6
    await arena.new('live', players=[dict(preset='value', name='自定义研究员',
        style='只研究经过验证的证据，无法判断时保持现金。', model='deepseek-flash')])
    p = arena.state['players'][0]
    assert p['name'] == '自定义研究员'
    assert p['style'] == p['strategy'] == '只研究经过验证的证据，无法判断时保持现金。'


@pytest.mark.parametrize('models', [[], ['deepseek-flash'] * 9, ['   ']])
async def test_invalid_roster_leaves_active_match_untouched(arena, models):
    before = copy.deepcopy(arena.state)
    with pytest.raises(ValueError):
        await arena.new('live', models=models)
    assert arena.state == before


@pytest.mark.parametrize('start_index,end_index,validation_index', [
    (32, 30, None), (30, 32, 30), (30, 32, 33),
])
async def test_invalid_learning_range_cannot_replace_match(arena, dataset,
        start_index, end_index, validation_index):
    before = copy.deepcopy(arena.state)
    with pytest.raises(ValueError):
        await arena.new('replay', dataset['dates'][start_index], end=dataset['dates'][end_index],
            validation_start=dataset['dates'][validation_index] if validation_index is not None else None)
    assert arena.state == before


async def test_full_range_autoplay_stops_at_selected_end_and_hides_future(arena, dataset, monkeypatch):
    observed = []
    async def policy(player, context, execute, emit):
        history = await execute('price_history', {'symbol': 'sh600519'})
        assert all(b[0] <= context['date'] for b in history['bars'])
        assert all(q['date'] <= context['date'] for q in context['observed_stocks'])
        observed.append(context['date'])
        return await learning_policy(player, context, execute, emit)
    monkeypatch.setattr(arena.agents, 'run', policy)
    start, end = dataset['dates'][30], dataset['dates'][33]
    await arena.new('replay', start, models=['deepseek-flash'], end=end)
    arena.running = True
    await asyncio.wait_for(arena.auto(), timeout=10)
    assert observed == dataset['dates'][30:34]
    assert arena.state['round'] == 4 and arena.state['day'] == end
    assert not arena.running and not arena.error
    before = copy.deepcopy(arena.state)
    with pytest.raises(ValueError):
        await arena.step('past-range-end')
    assert arena.state == before


async def test_validation_cannot_change_trained_strategy_or_notes(arena, dataset, monkeypatch):
    monkeypatch.setattr(arena.agents, 'run', learning_policy)
    await arena.new('replay', dataset['dates'][30], models=['deepseek-flash'],
        end=dataset['dates'][33], validation_start=dataset['dates'][32])
    await arena.step('training-first')
    await arena.step('training-second')
    trained = copy.deepcopy(arena.state['players'][0])
    await arena.step('validation-first')
    await arena.step('validation-second')
    validated = arena.state['players'][0]
    assert validated['strategy'] == trained['strategy']
    assert validated['notes'] == trained['notes']
    assert validated['versions'] == trained['versions']
    assert len(validated['history']) > len(trained['history'])
    with pytest.raises(ValueError):
        await arena.execute(arena.state, validated, {}, 'save_note', {'note': 'future leakage'})
    # Validation results remain inspectable without changing the deployable memory.
    assert arena.public()['learning']


async def test_promotion_carries_learning_but_resets_virtual_portfolio(arena, dataset, monkeypatch):
    monkeypatch.setattr(arena.agents, 'run', learning_policy)
    await arena.new('replay', dataset['dates'][30], models=['deepseek-flash'] * 2,
        end=dataset['dates'][32], validation_start=dataset['dates'][32])
    source_id = arena.state['id']
    for n in range(3):
        await arena.step('promotion-round-' + str(n))
    source = copy.deepcopy(arena.state)
    selected = source['players'][1]
    assert selected['positions'] and selected['versions']
    await arena.promote([selected['id']], title='验证后跟盘')
    live = arena.state
    p = live['players'][0]
    assert live['id'] != source_id and live['mode'] == 'live' and live['round'] == 0
    assert len(live['players']) == 1
    assert p['cash'] == INITIAL and p['positions'] == {} and p['orders'] == []
    assert p['realized'] == p['calls'] == p['tokens'] == 0
    assert p['strategy'] == selected['strategy'] and p['notes'] == selected['notes']
    assert p['versions'] == [] and len(p['history']) == 1
    assert p['history'][0]['equity'] == INITIAL
    assert p['provenance']['source_match_id'] == source_id
    assert p['provenance']['source_player_id'] == selected['id']
    assert p['provenance']['trained_until'] == dataset['dates'][31]
    assert p['provenance']['validation_from'] == dataset['dates'][32]
    assert p['provenance']['strategy_version'] == len(selected['versions'])
    assert p['learned_experience']['source_versions'] == selected['versions']
    saved = json.loads(arena.db.execute('SELECT payload FROM matches WHERE id=?', (source_id,)).fetchone()[0])
    assert saved == source
    await arena.load(source_id)
    assert arena.state == source


async def test_inherited_experience_has_model_facing_yuan_units(arena, dataset, monkeypatch):
    monkeypatch.setattr(arena.agents, 'run', learning_policy)
    await arena.new('replay', dataset['dates'][30], models=['deepseek-flash'], end=dataset['dates'][30])
    await arena.step('experience-unit-train')
    trained = copy.deepcopy(arena.state['players'][0])
    await arena.promote([trained['id']])
    observations = []
    async def inspect(player, context, execute, emit):
        inherited = context['inherited_experience']
        observations.append(inherited)
        # Compact deployable experience contains no internal-cent accounting records.
        assert 'source_versions' not in inherited
        assert set(inherited) == {'cutoff', 'version', 'strategy', 'summary', 'source_match_id'}
        assert inherited['version'] == len(trained['versions'])
        assert inherited['strategy'] == trained['strategy']
        return 'observed without new decision'
    monkeypatch.setattr(arena.agents, 'run', inspect)
    await arena.step('experience-unit-live')
    assert observations
    assert arena.state['players'][0]['learned_experience']['source_versions'] == trained['versions']


@pytest.mark.parametrize('validation_reviews', [True, False])
async def test_validation_evidence_is_based_on_separate_segment(arena, dataset, monkeypatch, validation_reviews):
    async def late_entry(player, context, execute, emit):
        if context['round'] == 2:
            await execute('place_order', dict(symbol='sh600519', side='buy', quantity=100,
                limit_price=12, reason='last training day fixture'))
        if context.get('learning_phase') == 'validation' and not validation_reviews:
            return '模型服务不可用，未完成验证决策'
        return (await execute('finish', dict(summary='review at ' + context['date'], risk='risk',
            lesson='lesson', next_strategy='strategy ' + context['date'])))['summary']
    monkeypatch.setattr(arena.agents, 'run', late_entry)
    await arena.new('replay', dataset['dates'][30], models=['deepseek-flash'],
        end=dataset['dates'][36], validation_start=dataset['dates'][32])
    for i in range(7):
        await arena.step('segmented-evidence-' + str(i))
    result = arena.public()['learning']
    metric = result['metrics'][0]
    assert result['stage'] == 'complete' and result['can_promote']
    assert metric['training']['rounds'] == 2 and metric['validation']['rounds'] == 5
    assert metric['training']['filled_orders'] == 0 and metric['validation']['filled_orders'] == 1
    assert metric['validation']['start_equity'] == metric['training']['end_equity']
    assert metric['validation']['completed_reviews'] == (5 if validation_reviews else 0)
    assert metric['evidence'] == ('tested' if validation_reviews else 'insufficient')
    assert len(arena.state['players'][0]['versions']) == 2
    checkpoint = arena.state['players'][0]['checkpoint']
    assert checkpoint['summary'] == 'review at ' + dataset['dates'][31]
    assert checkpoint['cutoff'] < dataset['dates'][32]


async def test_call_budget_pause_can_be_resumed_without_resetting_experiment(arena, dataset, monkeypatch):
    monkeypatch.setattr(arena.agents, 'run', learning_policy)
    await arena.new('replay', dataset['dates'][30], models=['deepseek-flash'], end=dataset['dates'][31])
    arena.agents.max_calls = 6
    before = copy.deepcopy(arena.state)
    with pytest.raises(ValueError, match='预算'):
        await arena.step('budget-blocked-round')
    assert arena.state == before
    arena.extend_budget(10)
    assert arena.agents.max_calls == 16 and not arena.running
    await arena.step('budget-after-extend')
    assert arena.state['id'] == before['id'] and arena.state['round'] == 1


async def test_checkpoint_cutoff_includes_notes_after_last_strategy_version(arena, dataset, monkeypatch):
    async def partial_review(player, context, execute, emit):
        if context['round'] == 2:
            await execute('save_note', {'note': 'information observed on ' + context['date']})
            return '本轮没有完成策略复盘'
        return await learning_policy(player, context, execute, emit)
    monkeypatch.setattr(arena.agents, 'run', partial_review)
    await arena.new('replay', dataset['dates'][30], models=['deepseek-flash'],
        end=dataset['dates'][32], validation_start=dataset['dates'][32])
    for i in range(3):
        await arena.step('partial-review-' + str(i))
    p = arena.state['players'][0]
    assert len(p['versions']) == 1
    assert p['checkpoint']['cutoff'] == dataset['dates'][31]
    assert p['checkpoint']['notes'][-1]['date'] == dataset['dates'][31]
    await arena.promote([p['id']])
    assert arena.state['players'][0]['provenance']['trained_until'] == dataset['dates'][31]


async def test_promotion_rejects_unfinished_or_unknown_selection(arena, dataset, monkeypatch):
    monkeypatch.setattr(arena.agents, 'run', learning_policy)
    await arena.new('replay', dataset['dates'][30], models=['deepseek-flash'], end=dataset['dates'][31])
    selected = arena.state['players'][0]['id']
    before = copy.deepcopy(arena.state)
    with pytest.raises(ValueError):
        await arena.promote([selected])
    assert arena.state == before
    await arena.step('gate-training-one')
    await arena.step('gate-training-two')
    complete = copy.deepcopy(arena.state)
    for invalid in ([], ['missing-agent'], [selected, selected]):
        with pytest.raises(ValueError):
            await arena.promote(invalid)
        assert arena.state == complete


async def test_completed_rounds_without_strategy_evidence_cannot_promote(arena, dataset, monkeypatch):
    async def failed_model(player, context, execute, emit):
        return '模型不可用，未产生策略复盘'
    monkeypatch.setattr(arena.agents, 'run', failed_model)
    await arena.new('replay', dataset['dates'][30], models=['deepseek-flash'], end=dataset['dates'][31])
    await arena.step('no-evidence-first')
    await arena.step('no-evidence-second')
    with pytest.raises(ValueError):
        await arena.promote([arena.state['players'][0]['id']])


async def test_old_three_player_archive_preserves_accounts_and_strategies(arena, monkeypatch):
    await arena.new('live', models=['deepseek-flash'] * 3)
    legacy = copy.deepcopy(arena.state)
    for key in ('replay_plan', 'learning', 'lineage', 'title'):
        legacy.pop(key, None)
    legacy['players'][0]['strategy'] = 'saved policy from earlier version'
    legacy['revision'] += 1
    arena.commit(legacy)
    saved_id = legacy['id']
    await arena.new('live')
    await arena.load(saved_id)
    assert len(arena.state['players']) == 3
    assert arena.state['players'] == legacy['players']
    assert len(arena.public()['players']) == 3
