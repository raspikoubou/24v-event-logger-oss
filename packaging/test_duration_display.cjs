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
  logging: true,
  fixtures: { channels: [{ id: 1, enabled: true, value: "OFF" }], events: [] },
  demoClockBaseMs: 0,
  loggingStartedAtMs: 0,
  demoNowMs: () => 886.199951171875,
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
// 初期API変換は外側のscopeで丸め、診断詳細はrunApp内の表示関数を使う。
for (const expression of [
  "Number(status.daemon.write_latency.max_delay_ms_5m || 0).toFixed(1)",
  "Number(status.daemon.write_latency.oldest_pending_ms || 0).toFixed(1)",
  "formatMilliseconds(fixtures.writeLatency.max_delay_ms_5m)",
  "formatMilliseconds(fixtures.writeLatency.oldest_pending_ms)",
]) assert(source.includes(expression));
console.log("millisecond rounding and realtime display checks passed");
