const { test, expect } = require('@playwright/test');
const fs = require('node:fs/promises');
const path = require('node:path');
const staticRoot = path.resolve(__dirname, '../../src/cryolens/web/static');
const storageKey = 'cryolens-reference:browser-fixture:primary:survey';
const packet = {
  workspace_id: 'browser-fixture', phase: 'primary', queue: 'survey',
  identity_claim_boundary: 'Synthetic review fixture; no scientific imagery.',
  tasks: [0, 1].map(i => ({
    task_id: `task-${i}`, mode: 'survey', shape: [64, 64], core: [0, 0, 64, 64],
    acquired_utc: '2020-01-01T00:00:00Z', spacing_m: 80, optical: [],
    HH: 'HH.svg', HV: 'HV.svg', centre: null,
  })),
};

async function openViewer(page, customPacket = packet) {
  await page.route('http://review.test/**', async route => {
    const name = new URL(route.request().url()).pathname.slice(1);
    if (name === 'tasks.json') return route.fulfill({ json: customPacket });
    if (name.endsWith('.svg')) return route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="gray"/></svg>',
    });
    const file = name || 'reference.html';
    if (!['reference.html', 'reference.js', 'reference.css'].includes(file)) return route.abort();
    return route.fulfill({ path: path.join(staticRoot, file) });
  });
  await page.goto('http://review.test/');
  await expect(page.locator('#progress')).toContainText('Task 1 of 2');
  await expect(page.locator('#image')).toHaveAttribute('width', '64');
}

async function inspect(page) {
  await page.locator('#band').selectOption('HH');
  // A loaded channel may be declared inspected only after its image onload.
  await page.waitForFunction(() => document.querySelector('#image').width === 64);
  await page.locator('#reviewer').fill('fixture-reviewer');
  await page.locator('#notes').fill('Inspected the whole synthetic valid core in both channels.');
  await page.locator('#status').selectOption('complete');
  await page.locator('#attest').check();
}

async function mark(page, x = 40, y = 40) {
  await page.locator('#image').click({ position: { x: x + 0.5, y: y + 0.5 } });
  await page.locator('#label').selectOption('target');
  await page.locator('#rationale').fill('Compact observable return; identity unknown.');
}

async function exported(page) {
  const download = page.waitForEvent('download');
  await page.locator('#export').click();
  return JSON.parse(await fs.readFile(await (await download).path(), 'utf8'));
}

test('Save records the explicitly selected point and exports it without Add-mark', async ({ page }) => {
  await openViewer(page); await inspect(page); await mark(page);
  await page.locator('button[type=submit]').click();
  await expect(page.locator('#message')).toContainText('saved locally');
  const [response] = await exported(page);
  expect(response.status).toBe('complete');
  expect(response.marks).toEqual([expect.objectContaining({ row: 40, col: 40, label: 'target', identity: 'unknown' })]);
  expect(response.viewed_channels.sort()).toEqual(['HH', 'HV']);
  await page.reload();
  await expect(page.locator('#marks')).toContainText('sample 40, 40');
});

test('unrecorded marks and notes survive navigation and reload as drafts only', async ({ page }) => {
  await openViewer(page); await inspect(page); await mark(page);
  await page.locator('#next').click(); await page.locator('#previous').click();
  await expect(page.locator('#position')).toHaveValue('40, 40');
  await expect(page.locator('#rationale')).toHaveValue('Compact observable return; identity unknown.');
  await page.reload();
  await expect(page.locator('#position')).toHaveValue('40, 40');
  await expect(page.locator('#notes')).toHaveValue('Inspected the whole synthetic valid core in both channels.');
  expect(await page.evaluate(key => localStorage.getItem(key), storageKey)).toBeNull();
  await page.locator('#export').click();
  await expect(page.locator('#message')).toContainText('No personally inspected responses');
});

test('invalid supporting evidence cannot silently save an empty complete survey', async ({ page }) => {
  await openViewer(page); await inspect(page); await mark(page);
  await page.locator('#support').fill('This looks like an iceberg');
  await page.locator('button[type=submit]').click();
  await expect(page.locator('#message')).toContainText('JSON array');
  expect(await page.evaluate(key => localStorage.getItem(key), storageKey)).toBeNull();
  await expect(page.locator('#position')).toHaveValue('40, 40');
  await page.locator('#support').fill('[]');
  await page.locator('button[type=submit]').click();
  expect((await exported(page))[0].marks).toHaveLength(1);
});

test('multiple marks are retained; saving an existing mark does not duplicate it', async ({ page }) => {
  await openViewer(page); await inspect(page); await mark(page, 20, 20);
  await page.locator('#add-mark').click(); await mark(page, 40, 40);
  await page.locator('button[type=submit]').click();
  expect((await exported(page))[0].marks).toHaveLength(2);
  await page.locator('button[type=submit]').click();
  expect((await exported(page))[0].marks).toHaveLength(2);
});

test('explicitly clearing a selected point allows a genuine empty survey', async ({ page }) => {
  await openViewer(page); await inspect(page); await mark(page);
  await page.locator('#clear-point').click();
  await page.locator('button[type=submit]').click();
  const [response] = await exported(page);
  expect(response.status).toBe('complete'); expect(response.marks).toEqual([]);
});

test('failed browser storage blocks navigation and offers an honest session export', async ({ page }) => {
  await openViewer(page); await inspect(page); await mark(page);
  await page.evaluate(() => { Storage.prototype.setItem = () => { throw new Error('quota'); }; });
  await page.locator('#notes').fill('Updated notes after storage failed.');
  await page.locator('#next').click();
  await expect(page.locator('#progress')).toContainText('Task 1 of 2');
  await expect(page.locator('#message')).toContainText('storage');
  await page.locator('button[type=submit]').click();
  await expect(page.locator('#message')).toContainText('storage');
  expect((await exported(page))[0].marks).toHaveLength(1);
});

test('changing the selected point requires recording or clearing the previous one', async ({ page }) => {
  await openViewer(page); await mark(page);
  await page.locator('#image').click({ position: { x: 20, y: 20 } });
  await expect(page.locator('#position')).toHaveValue('40, 40');
  await expect(page.locator('#message')).toContainText('Record');
});

test('candidate Save records the centre assessment; it never invents identity evidence', async ({ page }) => {
  const candidate = structuredClone(packet);
  candidate.tasks.forEach(task => { task.mode = 'candidate'; task.centre = [32, 32]; });
  await openViewer(page, candidate); await inspect(page);
  await page.locator('#label').selectOption('target');
  await page.locator('#rationale').fill('Observable centre return.');
  await page.locator('#identity').selectOption('iceberg');
  await page.locator('button[type=submit]').click();
  await expect(page.locator('#message')).toContainText('independent positive evidence');
  await page.locator('#identity').selectOption('unknown');
  await page.locator('button[type=submit]').click();
  expect((await exported(page))[0].marks).toEqual([expect.objectContaining({ row: 32, col: 32, identity: 'unknown' })]);
});

test('existing saved submissions remain byte-for-byte unchanged during draft recovery', async ({ page }) => {
  const previous = JSON.stringify({ 'task-0': {
    workspace_id: 'browser-fixture', task_id: 'task-0', phase: 'primary',
    reviewer_id: 'original-reviewer', reviewed_utc: '2026-10-01T12:00:00Z',
    status: 'complete', active_seconds: 90, personally_inspected: true,
    viewed_channels: ['HH', 'HV'], notes: 'An existing submitted response.',
    marks: [{ row: 12, col: 14, label: 'uncertain', identity: 'unknown', rationale: 'Original uncertainty.', supporting_observations: [] }],
    excluded_regions: [],
  }});
  await page.addInitScript(({ key, value }) => {
    if (!localStorage.getItem(key)) localStorage.setItem(key, value);
  }, { key: storageKey, value: previous });
  await openViewer(page);
  await expect(page.locator('#marks')).toContainText('Original uncertainty');
  await mark(page); await page.locator('#notes').fill('Draft edits only.');
  await page.locator('#next').click(); await page.locator('#previous').click();
  await page.reload();
  expect(await page.evaluate(key => localStorage.getItem(key), storageKey)).toBe(previous);
  expect(await exported(page)).toEqual(Object.values(JSON.parse(previous)));
});

test('missing rationale blocks Save and keeps selected point recoverable', async ({ page }) => {
  await openViewer(page); await inspect(page);
  await page.locator('#image').click({ position: { x: 40.5, y: 40.5 } });
  await page.locator('button[type=submit]').click();
  await expect(page.locator('#message')).toContainText('rationale');
  expect(await page.evaluate(key => localStorage.getItem(key), storageKey)).toBeNull();
  await page.reload(); await expect(page.locator('#position')).toHaveValue('40, 40');
});

test('an incomplete channel inspection still cannot be declared complete', async ({ page }) => {
  await openViewer(page); await mark(page);
  await page.locator('#reviewer').fill('fixture-reviewer');
  await page.locator('#notes').fill('Only HV inspected.');
  await page.locator('#status').selectOption('complete'); await page.locator('#attest').check();
  await page.locator('button[type=submit]').click();
  await expect(page.locator('#message')).toContainText('Inspect both HH and HV');
  expect(await page.evaluate(key => localStorage.getItem(key), storageKey)).toBeNull();
});

test('a changed candidate assessment is exported once with the latest explicit fields', async ({ page }) => {
  const candidate = structuredClone(packet);
  candidate.tasks.forEach(task => { task.mode = 'candidate'; task.centre = [32, 32]; });
  await openViewer(page, candidate); await inspect(page);
  await page.locator('#rationale').fill('Initially uncertain.'); await page.locator('#add-mark').click();
  await page.locator('#label').selectOption('target');
  await page.locator('#rationale').fill('After inspection, observable target.');
  await page.locator('button[type=submit]').click();
  const [response] = await exported(page);
  expect(response.marks).toEqual([expect.objectContaining({ label: 'target', rationale: 'After inspection, observable target.' })]);
});

test('another tab response is preserved and same-task conflicts cannot overwrite it', async ({ page }) => {
  await openViewer(page); await inspect(page); await mark(page);
  const other = { 'task-1': { task_id: 'task-1', notes: 'Other tab response', marks: [], excluded_regions: [] } };
  await page.evaluate(({ key, value }) => localStorage.setItem(key, JSON.stringify(value)), { key: storageKey, value: other });
  await page.locator('button[type=submit]').click();
  expect((await exported(page)).find(r => r.task_id === 'task-1')).toEqual(other['task-1']);
  await page.evaluate(key => {
    const value = JSON.parse(localStorage.getItem(key));
    value['task-0'].notes = 'Changed concurrently'; localStorage.setItem(key, JSON.stringify(value));
  }, storageKey);
  await page.locator('#notes').fill('My conflicting change');
  await page.locator('button[type=submit]').click();
  await expect(page.locator('#message')).toContainText('changed in another tab');
  expect(await page.evaluate(key => JSON.parse(localStorage.getItem(key))['task-0'].notes, storageKey)).toBe('Changed concurrently');
});

test('a change-only candidate dropdown edit replaces the recorded assessment on Save', async ({ page }) => {
  const candidate = structuredClone(packet);
  candidate.tasks.forEach(task => { task.mode = 'candidate'; task.centre = [32, 32]; });
  await openViewer(page, candidate); await inspect(page);
  await page.locator('#rationale').fill('Candidate inspected; later assessment revised.');
  await page.locator('#add-mark').click();
  await page.locator('#label').evaluate(select => {
    select.value = 'target'; select.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await page.locator('button[type=submit]').click();
  expect((await exported(page))[0].marks).toEqual([expect.objectContaining({ label: 'target', row: 32, col: 32 })]);
});
