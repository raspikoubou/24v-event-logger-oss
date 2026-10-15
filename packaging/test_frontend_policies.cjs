"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const web = path.join(__dirname, "../src/event_logger/web");
let time = 0;
const context = { window: {}, performance: { now: () => time }, liveMode: true, logging: true,
  fixtures: { channels: [{ id: 1, enabled: true, value: "ON" }], events: [],
    dashboardValues: [{ widget_id: "run", state: "ON", elapsed_ms: 1_200_000 }],
    dashboardReceivedAtMs: 0 } };
vm.createContext(context);
for (const moduleName of ["refresh_policy", "retention_policy", "storage_policy", "csv_format"]) {
  vm.runInContext(fs.readFileSync(path.join(web, "static", moduleName + ".js"), "utf8"), context);
}

const retention = context.window.EventLoggerRetentionPolicy;
assert.equal(retention.retentionDaysFromSelectValue(""), null);
assert.equal(retention.retentionDaysFromStatus({ retention_days: null }), null);
assert.equal(retention.retentionDaysFromStatus({}), 365);
const storage = context.window.EventLoggerStoragePolicy;
assert.equal(storage.storageWarningFor({ status: "critical", free_bytes: 0 }).freeBytes, 0);
assert.equal(storage.storageWarningFor({ status: "critical", free_bytes: null }).freeBytes, null);
assert.equal(storage.storageWarningFor({ status: "critical", free_bytes: 42 }, true).freeBytes, null);
assert.deepEqual({ ...storage.warningVisibilityFor({ count: 0, storageVisible: true }) },
  { syncVisible: false, visible: true });
const csv = context.window.EventLoggerCsvFormat;
for (const value of ["=1+1", " +123", "-123", "@text", "\ttext", "\r=1+1"]) {
  assert.equal(csv.csvCell(value), `"'${value}"`);
}
assert.equal(csv.csvCell("工程A"), '"工程A"');

const dashboard = fs.readFileSync(path.join(web, "frontend/parts/02_dashboard.jsfrag"), "utf8");
const begin = dashboard.indexOf("  function formatMilliseconds(value) {");
const end = dashboard.indexOf("  function updateCurrentStateCard(", begin);
assert(begin >= 0 && end > begin);
vm.runInContext(dashboard.slice(begin, end), context);
assert.equal(context.formatDuration(3.999999523162842), "4.0ミリ秒");
assert.equal(context.currentStateValues("IN1", "運転", "停止", "run").elapsed, "20分 0秒継続");
time = 5_000;
assert.equal(context.currentStateValues("IN1", "運転", "停止", "run").elapsed, "20分 5秒継続");
context.fixtures.dashboardValues = [{ widget_id: "run", state: "UNKNOWN" }];
assert.equal(context.currentStateValues("IN1", "運転", "停止", "run").value, "UNKNOWN");
assert.equal(context.currentStateValues("IN1", "運転", "停止", "run").elapsed, "継続時間は計測していません");

async function main() {
  const policy = context.window.EventLoggerRefreshPolicy;
  assert.equal(policy.shouldRefreshDashboard({ currentPeriod: "custom",
    customRange: { from_ms: 0, to_ms: 1_000 }, liveMode: true,
    dashboardLoaded: true, nextRefreshAtMs: 2_000, nowMs: 3_000 }), false);
  const pending = [];
  let calls = 0;
  const refresh = policy.createRefreshCoordinator(async () => {
    calls += 1;
    await new Promise((resolve) => pending.push(resolve));
  });
  const first = refresh();
  const trailing = refresh(true);
  assert.equal(calls, 1);
  pending.shift()();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls, 2);
  pending.shift()();
  await Promise.all([first, trailing]);
  console.log("frontend policy and CSV checks passed");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
