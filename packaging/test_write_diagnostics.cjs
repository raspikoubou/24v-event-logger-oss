"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/event_logger/web/static/app.js'), 'utf8');
const policyContext = {window: {}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/event_logger/web/static/storage_policy.js'), 'utf8'), policyContext);
const policy = policyContext.window.EventLoggerStoragePolicy;
const start = source.indexOf('  function renderUnsynchronizedWarnings()');
const end = source.indexOf('  function selectedTimeZone()', start);
for (const severity of ['normal', 'attention', 'warning']) {
  const nodes = new Map();
  function node(key) {
    if (!nodes.has(key)) nodes.set(key, {hidden: false, textContent: '', classList: {toggle() {}}, querySelector(s) {return node(key + s);}});
    return nodes.get(key);
  }
  const context = {document: {querySelector: node}, fixtures: {unsynchronizedCount: 0, writeLatency: {severity, warning: severity === 'warning', delayed_count_5m: 7, max_delay_ms_5m: 2702, oldest_pending_ms: 0}}, liveMode: false,
    storageWarningFor: () => ({visible: false, title: ''}), storageWarningMessage: () => '',
    warningVisibilityFor: policy.warningVisibilityFor, formatMilliseconds: v => Number(v).toFixed(1)};
  vm.runInNewContext(source.slice(start, end) + '\nrenderUnsynchronizedWarnings();', context);
  assert.equal(node('.banner--warning').hidden, severity !== 'warning');
  assert.equal(node('#write-latency-warning').hidden, severity === 'normal');
  if (severity === 'attention') assert.match(node('#write-latency-warningspan').textContent, /7回.*2702\.0ms/);
  if (severity === 'warning') assert.match(node('#write-latency-warningspan').textContent, /確認してください/);
}
console.log('normal, attention and warning GUI visibility checks passed');
