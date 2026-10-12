"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/event_logger/web/static/app.js"), "utf8");
function helper(name, next) {
  const start = source.indexOf(`  function ${name}(`);
  const end = source.indexOf(`  function ${next}(`, start);
  assert(start >= 0 && end > start);
  return source.slice(start, end);
}
const context = {
  liveMode: false,
  fixtures: { dashboardPeriods: { shift: { label: "直近8時間" } }, events: [] },
  periodKeyForWidget: () => "shift",
  periodBounds: () => ({ startMs: 0, endMs: 100 }),
  signalIds: (value) => value.split(","),
  escapeHtml: (value) => String(value),
  timelineMarksHtml: () => '<button data-timeline-event></button>',
  timelineZoomLevels: [1, 1.5, 2],
};
vm.runInNewContext(helper("miniTimelineHtml", "historyWidgetBodyHtml")
  + helper("historyWidgetBodyHtml", "renderHistoryWidgetCard")
  + helper("renderHistoryWidgetCard", "renderCustomHistoryWidgets")
  + "this.html=historyWidgetBodyHtml; this.zoom=applyMiniTimelineZoom; this.render=renderHistoryWidgetCard;", context);
// 複数信号でもスクロール領域は一つだけ。ラベルはその外側に置く。
const html = context.html({ type: "タイムライン", channelId: "IN1,IN2,OUT6" });
assert.equal((html.match(/data-mini-timeline-scroll/g) || []).length, 1);
assert.equal((html.match(/data-mini-timeline-content/g) || []).length, 1);
assert.equal((html.match(/data-mini-timeline-track/g) || []).length, 3);
assert(html.indexOf("OUT6") < html.indexOf("data-mini-timeline-scroll"));
const content = { style: {} };
let scroll = { scrollLeft: 240 };
const label = {};
const out = {};
const into = {};
const body = {};
Object.defineProperty(body, "innerHTML", { set: () => { scroll = { scrollLeft: 0 }; } });
const elements = {
  "[data-mini-zoom-label]": label,
  "[data-mini-timeline-content]": content,
  '[data-mini-zoom="out"]': out,
  '[data-mini-zoom="in"]': into,
  "[data-history-widget-body]": body,
};
const card = {
  dataset: { miniZoomIndex: "2", widgetType: "タイムライン", widgetChannel: "IN1,IN2" },
  querySelector: (selector) => selector === "[data-mini-timeline-scroll]" ? scroll : elements[selector],
};
context.zoom(card);
assert.equal(content.style.width, "200%");
assert.equal(scroll.scrollLeft, 240);
assert.equal(into.disabled, true);
context.document = { activeElement: null, querySelector: () => null };
context.pinnedTimelineEvent = null;
context.shouldDeferTimelineWidgetRender = () => false;
context.bindTimelineEvents = () => {};
context.render(card);
assert.equal(scroll.scrollLeft, 240, "定期更新後も全行の共通位置を保持する");
card.dataset.miniZoomIndex = "1";
context.zoom(card);
assert.equal(content.style.width, "150%");
card.dataset.miniZoomIndex = "0";
context.zoom(card);
assert.equal(content.style.width, "100%");
assert.equal(scroll.scrollLeft, 0);
assert.equal(out.disabled, true);
console.log("shared timeline scroll checks passed");
