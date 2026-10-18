"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/event_logger/web/static/app.js'), 'utf8');
const template = fs.readFileSync(path.join(__dirname, '../src/event_logger/web/templates/index.html'), 'utf8');
const start = source.indexOf('  function renderUnsynchronizedWarnings()');
const end = source.indexOf('  function selectedTimeZone()', start);
// 全画面のタイトルより上に実体を一つだけ置き、旧来の重複表示を復活させない。
for (const id of ['unsynchronized-warning', 'write-latency-warning', 'configuration-sync-warning', 'storage-warning-detail']) {
  assert.equal(template.split(`id="${id}"`).length - 1, 1);
  assert.ok(template.indexOf(`id="${id}"`) < template.indexOf('id="view-dashboard"'));
}
for (const id of ['storage-warning-banner', 'diagnostic-warning-count']) assert.ok(!template.includes(`id="${id}"`));
assert.ok(!template.includes('class="banner banner--warning"'));
const nodes = new Map();
/**
 * 指定keyのDOM mockを遅延生成し、warning描画結果をテストから読み取れるようにする。
 * @param {string} key selectorまたはselector由来のnode key。
 * @returns {Object} hidden/textContent/classList/querySelectorを持つmock node。
 */
function node(key) {
  if (!nodes.has(key)) nodes.set(key, {hidden: false, textContent: '', classList: {/** @param {string} _name class名。 @param {boolean} _enabled 切替状態。 @returns {void} class変更を省略するDOM mock。 */ toggle() {}}, /** @param {string} s 子要素selector。 @returns {Object} keyを連結した子mock node。 */ querySelector(s) {return node(key + s);}});
  return nodes.get(key);
}
const fixtures = {unsynchronizedCount: 0, writeLatency: {severity: 'normal', warning: false, delayed_count_5m: 7, max_delay_ms_5m: 2702, oldest_pending_ms: 0}};
let storage = {visible: false, title: '', severity: 'normal'};
const context = {document: {querySelector: node}, fixtures, liveMode: true, activeView: 'diagnostics',
  /** @returns {Object} 個別テストで差し替えるstorage warning fixture。 */ storageWarningFor: () => storage, /** @returns {string} 表示用の固定容量注意文。 */ storageWarningMessage: () => '容量を確認してください',
  /** @param {number} v 時間値（ミリ秒）。 @returns {string} 小数1桁の表示値。 */ formatMilliseconds: v => Number(v).toFixed(1)};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
/**
 * 指定画面をactive viewにしてwarning描画関数を実行する。
 * @param {string} view 表示中として扱う画面名。
 * @returns {void} mock nodeのhidden/textContentを更新する。
 */
function render(view) {
  context.activeView = view;
  vm.runInContext('renderUnsynchronizedWarnings();', context);
}
for (const severity of ['normal', 'attention', 'warning']) {
  fixtures.writeLatency.severity = severity;
  fixtures.writeLatency.warning = severity === 'warning';
  for (const view of ['dashboard', 'logs', 'settings', 'diagnostics']) {
    render(view);
    const visible = severity === 'warning' || (severity === 'attention' && view === 'diagnostics');
    assert.equal(node('#system-notices').hidden, !visible);
    assert.equal(node('#write-latency-warning').hidden, !visible);
    assert.equal(node('#write-latency-warningspan').hidden, view !== 'diagnostics');
    assert.equal(node('#notice-details-link').hidden, !visible || view === 'diagnostics');
    if (severity === 'attention') assert.match(node('#write-latency-warningspan').textContent, /7回.*2702\.0ms/);
    if (severity === 'warning') assert.match(node('#write-latency-warningspan').textContent, /確認してください/);
  }
}
// 同時発生した別種の注意を落とさず、詳細と再読込は診断画面だけで表示する。
fixtures.unsynchronizedCount = 12;
fixtures.configurationSync = {outOfSync: true, message: '設定が一致していません'};
storage = {visible: true, title: 'SD空き容量が少なくなっています', severity: 'critical'};
for (const view of ['dashboard', 'diagnostics', 'settings', 'diagnostics']) {
  render(view);
  assert.equal(node('#system-notices').hidden, false);
  for (const id of ['unsynchronized-warning', 'configuration-sync-warning', 'storage-warning-detail']) {
    assert.equal(node('#' + id).hidden, false);
    assert.equal(node('#' + id + 'span').hidden, view !== 'diagnostics');
  }
  assert.match(node('#unsynchronized-warningstrong').textContent, /12件/);
  assert.match(node('#configuration-sync-warningspan').textContent, /一致していません/);
  assert.equal(node('#retry-config-sync').hidden, view !== 'diagnostics');
}
context.liveMode = false;
render('diagnostics');
assert.equal(node('#retry-config-sync').hidden, true);
fixtures.unsynchronizedCount = 0;
fixtures.configurationSync.outOfSync = false;
fixtures.writeLatency = {severity: 'normal', warning: false};
storage.visible = false;
render('diagnostics');
assert.equal(node('#system-notices').hidden, true);
assert.equal(node('#notice-details-link').hidden, true);
assert.ok(source.includes('activeView = viewName;\n    renderUnsynchronizedWarnings();'));
console.log('Notice placement, deduplication, simultaneous conditions and view transitions passed');
