// Integration QA only: requires tests/browser_server.py on port 8011.
// It refuses to run against any server that is not explicitly the isolated fixture.
const {chromium, expect}=require('@playwright/test');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const base='http://127.0.0.1:8011';

(async()=>{
  const identity=await fetch(base+'/__test__/identity').then(r=>r.json());
  assert.equal(identity.fixture,true);
  assert.equal(identity.external_network,false);
  assert.equal(identity.real_models,false);
  assert.equal(identity.temporary_database,true);
  await fetch(base+'/__test__/reset',{method:'POST'}).then(r=>{assert.equal(r.ok,true);});
  fs.mkdirSync('test-results',{recursive:true});
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1050}});
  const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const read=()=>page.evaluate(()=>fetch('/api/state').then(r=>r.json()));
  const waitState=predicate=>expect.poll(async()=>predicate(await read()),{timeout:30000}).toBe(true);
  const flows=[];
  try{
    await page.goto(base);
    await expect(page.locator('#connection')).toHaveText('本地服务已连接');
    const initial=await read();
    assert.equal(initial.players.length,6);
    assert.match(initial.source,/FIXTURE/);
    const dates=initial.dates;
    await page.locator('#new-button').click();
    await expect(page.locator('#new-dialog')).toBeVisible();
    await page.locator('input[name=roster_mode][value=inline]').check();
    await page.locator('#experiment-name').fill('TEST FIXTURE · 2日训练 + 5日验证');
    await page.locator('#start-date').selectOption(dates[30]);
    await page.locator('#validation-date').selectOption(dates[32]);
    await page.locator('#end-date').selectOption(dates[36]);
    while(await page.locator('.roster-edit').count()>2){
      await page.locator('[data-remove]').last().click();
    }
    await page.getByLabel('选手 1 名称',{exact:true}).fill('测试价值选手');
    await page.locator('.roster-edit').first().locator('.strategy-editor summary').click();
    await page.getByLabel('选手 1 策略',{exact:true}).fill('TEST FIXTURE custom strategy; historical observations only.');
    await page.getByLabel('选手 2 名称',{exact:true}).fill('测试趋势选手');
    await expect(page.locator('#range-preview')).toContainText('训练 2 日');
    await expect(page.locator('#range-preview')).toContainText('验证 5 日');
    await page.locator('#new-form button[type=submit]').click();
    await expect(page.locator('#new-dialog')).not.toBeVisible();
    await expect(page.locator('#experiment-title')).toHaveText('TEST FIXTURE · 2日训练 + 5日验证');
    const source=await read();
    assert.equal(source.players.length,2);
    assert.equal(source.learning.total_rounds,7);
    assert.match(source.players[0].style,/custom strategy/);
    flows.push('custom-two-agent-seven-day-experiment');

    await page.locator('#run').click();
    await waitState(s=>s.round>=1&&s.runtime.running);
    await expect(page.locator('#run')).toContainText('暂停');
    await page.locator('#run').click();
    await waitState(s=>!s.runtime.busy&&!s.runtime.running);
    const paused=await read();
    assert(paused.round>=1&&paused.round<7,'Paused state: '+JSON.stringify({id:paused.id,round:paused.round,runtime:paused.runtime}));
    await page.waitForTimeout(1200);
    assert.equal((await read()).round,paused.round);
    flows.push('pause-preserves-completed-progress');

    await page.locator('#run').click();
    await waitState(s=>s.learning.stage==='complete'&&!s.runtime.running);
    const completed=await read();
    assert.equal(completed.id,source.id);
    assert.equal(completed.day,dates[36]);
    assert.equal(completed.round,7);
    for(const p of completed.players){
      assert.equal(p.versions.length,2);
      assert.equal(p.notes.length,2);
      assert.equal(p.checkpoint.cutoff,dates[31]);
      assert.equal(p.strategy,p.checkpoint.strategy);
      assert.deepEqual(p.notes,p.checkpoint.notes);
      assert.equal(p.learning_metric.training.rounds,2);
      assert.equal(p.learning_metric.validation.rounds,5);
      assert.equal(p.learning_metric.validation.completed_reviews,5);
      assert.equal(p.learning_metric.validation.filled_orders,1);
      assert.equal(p.learning_metric.evidence,'tested');
    }
    flows.push('resume-completes-exact-range','server-enforced-validation-memory-freeze','separate-validation-evidence');

    await expect(page.locator('#run')).toContainText('查看策略评估');
    await page.locator('#run').click();
    await expect(page.locator('#experience-view')).toBeVisible();
    await expect(page.locator('.experience-card')).toHaveCount(2);
    await expect(page.locator('.experience-card').first()).toContainText('5 个验证交易日');
    await expect(page.locator('.experience-card').first()).toContainText('已有验证样本');
    await page.screenshot({path:'test-results/workflow-fixture-experience.png',fullPage:true});
    flows.push('experience-library-shows-checkpoint-and-evidence');
    await page.locator('#experience-view [data-player]').first().click();
    await expect(page.locator('#detail-dialog')).toBeVisible();
    await expect(page.locator('#detail-content')).toContainText('验证复盘');
    await expect(page.locator('#detail-content')).toContainText('TEST FIXTURE review on '+dates[36]);
    await page.locator('#detail-dialog [data-close]').click();
    flows.push('validation-reviews-visible-with-frozen-policy');

    await page.locator('#experience-view [data-promote]').click();
    await expect(page.locator('#promote-dialog')).toBeVisible();
    await expect(page.locator('#promotion-roster input:checked')).toHaveCount(2);
    await page.locator('#promotion-roster input').last().uncheck();
    await expect(page.locator('#promotion-count')).toContainText('已选择 1 位');
    await page.locator('#confirm-promote').click();
    await expect(page.locator('#promote-dialog')).not.toBeVisible();
    const live=await read();
    assert.equal(live.mode,'live');
    assert.notEqual(live.id,source.id);
    assert.equal(live.source_match_id,source.id);
    assert.equal(live.players.length,1);
    const p=live.players[0];
    assert.equal(p.cash,100_000_000);
    assert.equal(p.equity,100_000_000);
    assert.equal(p.orders.length,0);
    assert.equal(p.holdings.length,0);
    assert.equal(p.calls,0);
    assert.equal(p.versions.length,0);
    assert.equal(p.strategy,completed.players[0].checkpoint.strategy);
    assert.equal(p.provenance.source_match_id,source.id);
    assert.equal(p.provenance.trained_until,dates[31]);
    assert.equal(live.runtime.running,false);
    flows.push('select-one-agent-promote-to-fresh-million-account');

    await page.locator('#navigation [data-view=experience]').click();
    await expect(page.locator('#experience-view')).toBeVisible();
    await expect(page.locator('.experience-card')).toHaveCount(1);
    const sourceLink=page.locator('#experience-view [data-load="'+source.id+'"]');
    await expect(sourceLink).toContainText('TEST FIXTURE');
    await page.locator('#experience-view [data-player]').click();
    await expect(page.locator('#detail-dialog')).toBeVisible();
    await expect(page.locator('#detail-content')).toContainText('经验来自');
    await expect(page.locator('#detail-content')).toContainText('¥1,000,000.00');
    await page.screenshot({path:'test-results/workflow-fixture-provenance.png'});
    await page.locator('#detail-dialog [data-close]').click();
    flows.push('live-experience-provenance-visible');
    await sourceLink.click();
    await waitState(s=>s.id===source.id);
    const restored=await read();
    assert.deepEqual(restored.players,completed.players);
    assert.equal(restored.learning.stage,'complete');
    assert.equal(restored.runtime.running,false);
    flows.push('return-to-source-preserves-history-and-accounts');

    assert.deepEqual(errors,[]);
    const report={passed:true,fixture:true,external_network:false,real_model_calls:0,
      production_database_access:false,flows,errors,source_id:source.id,live_id:live.id};
    fs.writeFileSync('test-results/workflow-fixture-report.json',JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
