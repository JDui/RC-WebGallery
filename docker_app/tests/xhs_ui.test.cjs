const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function app() {
  const context = vm.createContext({ localStorage: { getItem: () => null, setItem() {} }, Date, Intl, URLSearchParams });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8'), context);
  return context.galleryApp();
}

test('first fetch limit defaults to 100 and supports unlimited without turning blank into zero', () => {
  const state = app();
  assert.equal(state.xhsNewSource.initial_fetch_limit, 100);
  assert.equal(state.xhsLimitValue('25'), 25);
  assert.equal(state.xhsLimitValue(''), null);
  assert.equal(state.xhsLimitValue(null), null);
  for (const value of [0, -1, 10001, 1.5, 'invalid']) assert.throws(() => state.xhsLimitValue(value));
});

test('an unsaved first-limit or anchor edit prevents imports until settings are saved', () => {
  const state = app();
  const source = { id: 1, anchor_count: 3, initial_fetch_limit: 100, config_version: 0, enabled: 1, refresh_enabled: 0, refresh_interval_minutes: 720 };
  state.xhsDrafts['1'] = state.xhsDraft(source);
  assert.equal(state.xhsSourceDirty(source), false);
  state.xhsDrafts['1'].initial_fetch_limit = '25';
  assert.equal(state.xhsSourceDirty(source), true);
  state.xhsDrafts['1'] = state.xhsDraft(source);
  state.xhsDrafts['1'].anchor_count = '5';
  assert.equal(state.xhsSourceDirty(source), true);
});

test('first-limit completion is described as partial and does not claim a full scan', () => {
  const state = app();
  assert.match(state.xhsStopText('first_sync_limit'), /部分完成/);
  assert.match(state.xhsStopText('manual_import'), /未核对完整/);
  assert.equal(state.taskStatusText('partial'), '部分完成');
  assert.equal(state.taskModeLabel('xhs-import'), '小红书导入');
});

test('refresh adopts a newer settings revision so a conflict does not trap the editor', async () => {
  const state = app();
  state.xhsDrafts['1'] = { config_version: 0, anchor_count: 3, initial_fetch_limit: 25, enabled: true };
  state.api = async () => ({ items: [{ id: 1, config_version: 1, anchor_count: 5, initial_fetch_limit: 100, enabled: 1 }] });
  await state.loadXhsSources();
  assert.equal(state.xhsDrafts['1'].config_version, 1);
  assert.equal(state.xhsDrafts['1'].anchor_count, 5);
});

test('XHS has its own gallery pill and switching clears a previous source selection', () => {
  const state = app();
  assert.ok(state.sourceKindOptions.some(option => option.key === 'xhs' && option.label === '小红书'));
  for (const method of ['closeGalleryLayers', 'scrollViewTop', 'refreshGalleryImmediately']) state[method] = () => {};
  state.selectedSubscriptionUids = ['site:1'];
  state.setSourceKind('xhs');
  assert.equal(state.sourceKind, 'xhs');
  assert.equal(state.selectedSubscriptionUids.length, 0);
  assert.equal(state.currentSectionTitle(), '小红书');
});

test('cancelled and partial XHS sync and archive verification tasks can be retried', () => {
  const state = app();
  for (const taskType of ['xhs-sync', 'xhs-verify']) {
    for (const status of ['cancelled', 'partial']) {
      assert.equal(state.canRetryTask({
        task_type: taskType,
        status,
        details: { retry_action: { kind: taskType } },
      }), true);
    }
  }
  assert.equal(state.canRetryTask({
    task_type: 'xhs-verify', status: 'partial', details: {},
  }), false);
  assert.equal(state.canRetryTask({
    task_type: 'pull', status: 'cancelled', details: { retry_action: { kind: 'pull' } },
  }), false);
});
