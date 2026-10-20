"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const context = { window: {} };
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, "../src/event_logger/web/static/ui_state.js"), "utf8"),
  context
);
const ui = context.window.EventLoggerUiState;

/** @returns {Promise<void>} Controller behavior is verified through its public APIs. */
async function main() {
  const logs = ui.createLogController();
  let now = 100;
  const pending = [];
  const first = logs.loadPage({
    fetchPage: () => new Promise((resolve) => pending.push(resolve)),
    pageSize: () => 2, now: () => now, onChange: () => {}
  });
  logs.resetQuery();
  const second = logs.loadPage({
    fetchPage: () => new Promise((resolve) => pending.push(resolve)),
    pageSize: () => 2, now: () => now, onChange: () => {}
  });
  pending[0]({ from_ms: 1, to_ms: 2, total_count: 1, items: [{ signal: "OLD" }] });
  await first;
  assert.equal(logs.loaded, false, "a response from the previous search cannot replace current state");
  pending[1]({ from_ms: 10, to_ms: 20, total_count: 1, items: [{ id: "event:7", signal: "IN1", value: "ON", clock_synced: true }] });
  await second;
  assert.equal(logs.events[0].id, 7);
  assert.equal(logs.searchRange.fromMs, 10);
  assert.equal(logs.loaded, true);
  assert.equal(logs.nextRefreshAtMs, 5_100);

  logs.setPage(4);
  const requestedPages = [];
  await logs.loadPage({
    fetchPage: async (page) => {
      requestedPages.push(page);
      return page === 4
        ? { from_ms: 10, to_ms: 20, total_count: 3, items: [] }
        : { from_ms: 10, to_ms: 20, total_count: 3, items: [{ signal: "IN2" }] };
    },
    pageSize: () => 1, now: () => now, onChange: () => {}
  });
  assert.deepEqual(requestedPages, [4, 3], "shrinking result sets retry on the last valid page");
  assert.equal(logs.page, 3);
  assert.equal(logs.events[0].signal, "IN2");

  const editor = ui.createWidgetEditorState();
  editor.setEditingRow({ id: "widget-1" });
  editor.markDirty();
  assert.equal(editor.shouldApplyServerWidgets(false), false);
  assert.equal(editor.shouldApplyServerWidgets(true), false);
  editor.clearDirty();
  assert.equal(editor.shouldApplyServerWidgets(true), false, "an open form remains protected from polling");
  assert.equal(editor.shouldApplyServerWidgets(false), true);

  const refreshes = [];
  const resolvers = [];
  const live = ui.createLiveStateController((force) => {
    refreshes.push(force);
    return new Promise((resolve) => resolvers.push(resolve));
  });
  const initial = live.refresh();
  live.refresh();
  live.refresh(true);
  assert.deepEqual(refreshes, [false], "ordinary polling refreshes stay serialized without a trailing call");
  resolvers.shift()({ revision: 1 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(refreshes, [false, true], "an explicit forced refresh is replayed after the active snapshot");
  resolvers.shift()({ revision: 2 });
  await initial;
  assert.equal(live.snapshot.revision, 2, "controller retains the newest server snapshot");
  console.log("UI state controller checks passed");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
