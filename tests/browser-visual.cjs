// Read-only visual QA. Requires an already-completed historical experiment.
// Never creates experiments, runs Agents, saves settings, or writes evidence.
// ALPHA_VISUAL_BASE=http://127.0.0.1:8011 can target the isolated fixture.
const {chromium, expect} = require('@playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const base = process.env.ALPHA_VISUAL_BASE || 'http://127.0.0.1:8010';
const origin = new URL(base).origin;
assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'Only local QA servers are supported');
const output = 'test-results';
const views = ['overview', 'history', 'live', 'agents', 'experience', 'connections', 'tools', 'settings'];
const sizes = [
  {name: 'desktop', width: 1440, height: 1000},
  {name: 'laptop', width: 1280, height: 800},
  {name: 'mobile', width: 390, height: 844, isMobile: true, hasTouch: true},
];
const report = {passed: false, base, read_only: true, checks: [], screenshots: [], errors: [], blocked_mutations: []};

async function read(endpoint) {
  const response = await fetch(base + '/api' + endpoint);
  assert(response.ok, `Read failed: ${endpoint} (${response.status})`);
  return response.json();
}

async function snapshot() {
  const [state, profiles, connections, settings, evidence] = await Promise.all([
    read('/state'), read('/profiles'), read('/connections'), read('/settings'), read('/evidence'),
  ]);
  return {
    state: {
      id: state.id, mode: state.mode, round: state.round, day: state.day,
      players: state.players, benchmark: state.benchmark,
      replay_plan: state.replay_plan, runtime_settings: state.runtime_settings,
    },
    profiles, connections, settings, evidence,
  };
}

async function settled(page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

async function noOverflow(page, label) {
  const metrics = await page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const scrollWidth = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
    const visibleEscapes = [...document.querySelectorAll('main > *, dialog[open]')]
      .filter(el => el.getClientRects().length)
      .map(el => ({element: el.id || el.className, rect: el.getBoundingClientRect().toJSON()}))
      .filter(item => item.rect.left < -1 || item.rect.right > width + 1);
    return {width, scrollWidth, visibleEscapes};
  });
  assert(metrics.scrollWidth <= metrics.width + 1, `${label}: horizontal page overflow ${JSON.stringify(metrics)}`);
  assert.equal(metrics.visibleEscapes.length, 0, `${label}: content outside viewport ${JSON.stringify(metrics.visibleEscapes)}`);
  return metrics;
}

async function capture(page, name, fullPage = true) {
  await settled(page);
  await noOverflow(page, name);
  const filename = path.join(output, `visual-${name}.png`);
  await page.screenshot({path: filename, fullPage, animations: 'disabled'});
  report.screenshots.push(filename);
}

async function navigate(page, view, size) {
  if (size.isMobile) await page.locator('#menu-button').click();
  await page.locator(`#navigation [data-view="${view}"]`).click();
  await expect(page.locator(`#navigation [data-view="${view}"]`)).toHaveAttribute('aria-current', 'page');
  if (['agents', 'connections', 'tools', 'settings'].includes(view)) {
    await expect(page.locator('#admin-view')).toBeVisible();
    await expect(page.locator('#admin-view .admin-toolbar h2').first()).toBeVisible();
  } else if (view === 'overview') {
    await expect(page.locator('#live-overview')).toBeVisible();
    await expect(page.locator('#dashboard-view')).toBeHidden();
    await expect(page.locator('#live-stock-price')).not.toHaveText('—');
  } else if (view === 'experience') {
    await expect(page.locator('#experience-view')).toBeVisible();
  } else if (view === 'live') {
    await expect(page.locator('#context-banner')).toBeVisible();
  } else {
    await expect(page.locator('#dashboard-view')).toBeVisible();
  }
  await settled(page);
  if (size.isMobile) await expect(page.locator('#sidebar')).not.toHaveClass(/\bopen\b/);
}

async function dialogCheck(page, selector, name, closeSelector) {
  const dialog = page.locator(selector);
  await expect(dialog).toBeVisible();
  await settled(page);
  const geometry = await dialog.evaluate(el => {
    const rect = el.getBoundingClientRect();
    return {
      left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
      width: el.clientWidth, scrollWidth: el.scrollWidth,
      viewportWidth: innerWidth, viewportHeight: innerHeight,
    };
  });
  assert(geometry.scrollWidth <= geometry.width + 1, `${name}: dialog horizontal overflow ${JSON.stringify(geometry)}`);
  assert(geometry.top >= -1 && geometry.bottom <= geometry.viewportHeight + 1,
    `${name}: dialog extends beyond viewport ${JSON.stringify(geometry)}`);
  await capture(page, name, false);
  const submit = dialog.locator('button[type="submit"]');
  if (await submit.count()) {
    await submit.scrollIntoViewIfNeeded();
    await expect(submit).toBeInViewport();
    report.checks.push(`${name}-submit-reachable-without-saving`);
  }
  await page.locator(closeSelector).click();
  await expect(dialog).not.toBeVisible();
  report.checks.push(`${name}-fits-and-closes`);
}

async function chartCheck(page, size) {
  await expect(page.locator('#chart-empty')).toBeHidden();
  await expect(page.locator('#run')).toContainText('查看策略评估');
  const pixels = await page.locator('#equity-chart').evaluate(canvas => {
    const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    const colors = [...document.querySelectorAll('#legend i')].map(el => {
      const rgb = getComputedStyle(el).backgroundColor.match(/[\d.]+/g);
      return rgb && rgb.slice(0, 3).map(Number);
    }).filter(Boolean);
    let seriesPixels = 0;
    const columns = new Set();
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i + 3] < 180) continue;
      if (colors.some(rgb => rgb.every((value, channel) => Math.abs(pixels[i + channel] - value) <= 18))) {
        seriesPixels++;
        columns.add((i / 4) % canvas.width);
      }
    }
    return {width: canvas.width, height: canvas.height, seriesPixels, coveredColumns: columns.size};
  });
  assert(pixels.width > 100 && pixels.height > 100, 'Chart must have a real drawing surface');
  assert(pixels.seriesPixels > 80 && pixels.coveredColumns > pixels.width * 0.35,
    `Expected visible data series, not just an empty canvas: ${JSON.stringify(pixels)}`);
  await page.locator('.performance').screenshot({path: path.join(output, `visual-${size.name}-completed-chart.png`)});
  report.screenshots.push(path.join(output, `visual-${size.name}-completed-chart.png`));
  report.checks.push(`${size.name}-completed-experiment-visible-data-curves`);
  report.chart_pixels ||= {};
  report.chart_pixels[size.name] = pixels;
}

(async () => {
  fs.mkdirSync(output, {recursive: true});
  const initial = await read('/state');
  assert.equal(initial.mode, 'replay', 'Load a completed historical experiment before running read-only visual QA');
  assert.equal(initial.learning?.stage, 'complete', 'A completed experiment is required; this script never runs Agents');
  assert(!initial.runtime.running && !initial.runtime.busy, 'Pause background work before visual QA');
  assert(initial.players.some(player => player.history.length >= 3), 'Multi-day chart data is required');
  const initialSnapshot = await snapshot();
  report.experiment = {id: initial.id, rounds: initial.round, players: initial.players.length, stage: initial.learning.stage};
  const browser = await chromium.launch({channel: 'chrome', headless: true});
  try {
    for (const size of sizes) {
      const context = await browser.newContext({
        viewport: {width: size.width, height: size.height},
        isMobile: !!size.isMobile, hasTouch: !!size.hasTouch, reducedMotion: 'reduce',
      });
      await context.route('**/*', async route => {
        const request = route.request();
        if (!['GET', 'HEAD'].includes(request.method())) {
          report.blocked_mutations.push({method: request.method(), path: new URL(request.url()).pathname});
          await route.abort('blockedbyclient');
        } else if (/^https?:/.test(request.url()) && new URL(request.url()).origin !== origin) {
          report.errors.push(`${size.name}: unexpected external request ${new URL(request.url()).origin}`);
          await route.abort('blockedbyclient');
        } else await route.continue();
      });
      const page = await context.newPage();
      page.on('pageerror', error => report.errors.push(`${size.name}: ${error.message}`));
      page.on('console', message => {
        if (message.type() === 'error') report.errors.push(`${size.name}: ${message.text()}`);
      });
      try {
        await page.goto(base + '/#overview');
        await expect(page.locator('#app-content')).toBeVisible();
        await expect(page.locator('#connection')).toHaveText('本地服务已连接');
        for (const view of views) {
          await navigate(page, view, size);
          await capture(page, `${size.name}-${view}`);
          if (size.name === 'laptop' && view === 'overview') {
            await capture(page, 'laptop-overview-first-screen', false);
          }
          report.checks.push(`${size.name}-${view}-page`);
          if (view === 'overview') {
            await expect(page.locator('#market-strip')).toContainText('实时行情');
            report.checks.push(`${size.name}-overview-independent-live-quotes`);
          }
          if (view === 'history') {
            await chartCheck(page, size);
            await page.locator('[data-config-snapshot]').click();
            await dialogCheck(page, '#detail-dialog', `${size.name}-configuration-snapshot`, '[data-close="detail-dialog"]');
            await page.locator('#new-button').click();
            await dialogCheck(page, '#new-dialog', `${size.name}-experiment-dialog`, '[data-close="new-dialog"]');
          }
          if (view === 'agents') {
            await page.locator('[data-admin-action="edit-profile"]').first().click();
            await expect(page.locator('#admin-form [name=model]')).not.toHaveValue('');
            await dialogCheck(page, '#admin-dialog', `${size.name}-agent-editor`, '[data-admin-close]');
          }
          if (view === 'connections') {
            await page.locator('[data-admin-action="edit-connection"]').first().click();
            await expect(page.locator('#admin-form [name=api_key]')).toHaveValue('');
            await dialogCheck(page, '#admin-dialog', `${size.name}-connection-editor`, '[data-admin-close]');
          }
        }
      } catch (error) {
        await page.screenshot({path: path.join(output, `visual-${size.name}-failure.png`), fullPage: true}).catch(() => {});
        throw error;
      } finally {
        await context.close();
      }
    }
    assert.deepEqual(await snapshot(), initialSnapshot, 'Visual QA must not change experiment or saved configuration');
    report.checks.push('read-only-business-state-preserved');
    assert.deepEqual(report.blocked_mutations, [], 'UI attempted a forbidden mutation');
    assert.deepEqual(report.errors, [], 'Browser console or runtime error');
    report.passed = true;
  } finally {
    await browser.close();
  }
})().catch(error => {
  report.failure = error.message;
  process.exitCode = 1;
}).finally(() => {
  fs.mkdirSync(output, {recursive: true});
  fs.writeFileSync(path.join(output, 'visual-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({passed: report.passed, checks: report.checks.length, screenshots: report.screenshots.length,
    errors: report.errors, blocked_mutations: report.blocked_mutations, failure: report.failure}));
});
