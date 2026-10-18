"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "src/event_logger/web/static/app.js"), "utf8");
const start = source.indexOf("  function formatMilliseconds(value) {");
const end = source.indexOf("  function updateCurrentStateCard(", start);
assert(start >= 0 && end > start);
const context = {
  liveMode: false,
  logging: true,
  fixtures: { channels: [{ id: 1, enabled: true, value: "OFF" }], events: [] },
  demoClockBaseMs: 0,
  loggingStartedAtMs: 0,
  /** @returns {number} 小数ミリ秒表示を再現するdemo時刻。 */ demoNowMs: () => 886.199951171875,
};
vm.runInNewContext(source.slice(start, end) + "\nthis.ms=formatMilliseconds;this.duration=formatDuration;this.state=currentStateValues;", context);
// 実画面に出た浮動小数値、丸めの両方向、ゼロ、単位境界を検証する。
for (const [value, expected] of [
  [886.199951171875, "886.2"],
  [886.300048828125, "886.3"],
  [886.39990234375, "886.4"],
  [0, "0.0"],
  [12.34, "12.3"],
  [12.36, "12.4"],
  [999.999, "1000.0"],
]) {
  assert.equal(context.ms(value), expected);
  assert.equal(context.duration(value), `${expected}ミリ秒`);
}
assert.equal(context.duration(1000), "1秒");
assert.equal(context.duration(61000), "1分 1秒");
assert.equal(context.state("IN1", "ON", "OFF").elapsed, "886.2ミリ秒継続");

// 再読み込みで新しいJS contextを作っても、起点はサーバーのelapsed_ms。
/**
 * 指定状態とAPI計測値を持つ独立VMを作り、再読込直後の経過時間を再現する。
 * @param {string} state 入力状態値。
 * @param {number} elapsedMs サーバーが返した継続時間（ミリ秒）。
 * @param {number} receivedAtMs snapshot受信時刻（ミリ秒）。
 * @returns {Object} テスト対象のVMとfixtureを保持するcontext。
 */
function liveContext(state, elapsedMs, receivedAtMs = 100) {
  const live = {
    liveMode: true,
    logging: true,
    fixtures: {
      channels: [{ id: 1, enabled: true, value: state }],
      events: [],
      dashboardValues: [
        { widget_id: "other", state: "ON", elapsed_ms: 999_000 },
        { widget_id: "current", state, elapsed_ms: elapsedMs },
      ],
      dashboardReceivedAtMs: receivedAtMs,
    },
    now: receivedAtMs,
  };
  live.performance = { /** @returns {number} VM内の単調時刻（ミリ秒）。 */ now: () => live.now };
  vm.runInNewContext(source.slice(start, end) + "\nthis.state=currentStateValues;", live);
  return live;
}
/** @param {Object} live liveContextの結果。 @returns {Object} current widgetの表示状態。 */ const readState = (live) => live.state("IN1", "稼働", "待機", "current");
for (const state of ["ON", "OFF"]) {
  const live = liveContext(state, 65_000);
  assert.equal(readState(live).displayValue, state === "ON" ? "稼働" : "待機");
  assert.equal(readState(live).elapsed, "1分 5秒継続");
  for (let second = 1; second <= 4; second += 1) {
    live.now += 1000;
    assert.equal(readState(live).elapsed, `1分 ${5 + second}秒継続`);
  }
  const reloaded = liveContext(state, 69_000, 10);
  assert.equal(readState(reloaded).elapsed, readState(live).elapsed);
  // 次のAPI snapshotで補正し、前snapshotからの時間を二重加算しない。
  live.fixtures.dashboardValues[1].elapsed_ms = 70_000;
  live.fixtures.dashboardReceivedAtMs = 5100;
  live.now = 5100;
  assert.equal(readState(live).elapsed, "1分 10秒継続");
  // ON/OFF切替の起点もAPIだけから受け取る。
  live.fixtures.dashboardValues[1] = {
    widget_id: "current", state: state === "ON" ? "OFF" : "ON", elapsed_ms: 2000,
  };
  assert.equal(readState(live).displayValue, state === "ON" ? "待機" : "稼働");
  assert.equal(readState(live).elapsed, "2秒継続");
  live.now += 1000;
  assert.equal(readState(live).elapsed, "3秒継続");
}
const unknown = liveContext("UNKNOWN", 65_000);
assert.equal(readState(unknown).value, "UNKNOWN");
assert.equal(readState(unknown).elapsed, "継続時間は計測していません");
for (const condition of ["disabled", "stopped", "missing", "no_elapsed"]) {
  const live = liveContext("ON", 65_000);
  if (condition === "disabled") live.fixtures.channels[0].enabled = false;
  if (condition === "stopped") live.logging = false;
  if (condition === "missing") live.fixtures.dashboardValues = null;
  if (condition === "no_elapsed") delete live.fixtures.dashboardValues[1].elapsed_ms;
  assert.equal(readState(live).elapsed, "継続時間は計測していません");
}
// 古いログやブラウザの壁時計には依存せず、widget IDごとにAPI値を選ぶ。
assert.equal(liveContext("OFF", 886).state("IN1", "ON", "OFF", "current").elapsed, "886.0ミリ秒継続");
const policyContext = { window: {} };
vm.runInNewContext(fs.readFileSync(path.join(root, "src/event_logger/web/static/refresh_policy.js"), "utf8"), policyContext);
const refresh = policyContext.window.EventLoggerRefreshPolicy.shouldRefreshDashboard;
const policy = {
  liveMode: true, forceDashboard: false, dashboardLoaded: true,
  currentPeriod: "custom", customRange: { from_ms: 100, to_ms: 1000 },
  nextRefreshAtMs: 5000, hasCurrentStateWidgets: true,
};
// 閉じた検索期間でも現在状態だけは通常の5秒間隔で再取得する。
for (let second = 1; second < 5; second += 1) {
  assert.equal(refresh({ ...policy, nowMs: second * 1000 }), false);
}
assert.equal(refresh({ ...policy, nowMs: 5000 }), true);
assert.equal(refresh({ ...policy, nowMs: 5000, hasCurrentStateWidgets: false }), false);
assert.equal(refresh({ ...policy, nowMs: 5000, liveMode: false }), false);
assert(source.includes("window.setInterval(renderClock, 1000)"));
assert(source.includes("fixtures.dashboardReceivedAtMs = next.dashboardReceivedAtMs"));
assert(source.includes("id: card.dataset.widget"));
// 初期API変換は外側のscopeで丸め、診断詳細はrunApp内の表示関数を使う。
for (const expression of [
  "Number(status.daemon.write_latency.max_delay_ms_5m || 0).toFixed(1)",
  "Number(status.daemon.write_latency.oldest_pending_ms || 0).toFixed(1)",
  "formatMilliseconds(fixtures.writeLatency.max_delay_ms_5m)",
  "formatMilliseconds(fixtures.writeLatency.oldest_pending_ms)",
]) assert(source.includes(expression));
console.log("millisecond rounding and realtime display checks passed");
