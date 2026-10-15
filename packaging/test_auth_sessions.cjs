"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const parts = path.join(__dirname, "../src/event_logger/web/frontend/parts");

async function testExpiredSessionRedirect() {
  const source = fs.readFileSync(path.join(parts, "00_bootstrap.jsfrag"), "utf8");
  const start = source.indexOf("  async function liveJson(");
  const end = source.indexOf("  const { dashboardApiUrl", start);
  const destinations = [];
  const context = {
    document: { querySelector: () => null },
    window: { location: { assign: (url) => destinations.push(url) } },
    fetch: async () => ({ status: 401, ok: false }),
  };
  vm.runInNewContext(source.slice(start, end) + "\nthis.request = liveJson;", context);
  await assert.rejects(context.request("/api/status"), /ログインし直して/);
  assert.deepEqual(destinations, ["/login"]);
  context.fetch = async () => ({ status: 200, ok: true, json: async () => ({ data: "accepted" }) });
  assert.equal(await context.request("/api/status"), "accepted");
  assert.equal(destinations.length, 1);
}

async function testPendingWidgetsSavedBeforePasswordChange() {
  const source = fs.readFileSync(path.join(parts, "08_system_events.jsfrag"), "utf8");
  const end = source.indexOf('  document.querySelectorAll("[data-settings-target]")');
  let save;
  let authenticated = true;
  let widgetsSaved = false;
  const elements = {
    "#save-settings": { addEventListener: (_event, handler) => { save = handler; } },
    "#heartbeat-enabled": { on: false },
    "#heartbeat-output": { value: "1" },
    "#heartbeat-interval": { value: "5" },
    "#device-name": { value: "設備" },
    "#hostname": { value: "event-logger-test" },
    "#password-protection": { on: true },
    "#shared-password": { value: "replacement-password" },
    "#retention-days": { value: "365" },
    "#timezone": { value: "Asia/Tokyo" },
  };
  const context = {
    liveMode: true,
    document: { querySelector: (selector) => elements[selector], querySelectorAll: () => [] },
    fixtures: { outputRules: [], settings: { passwordProtection: true } },
    switchIsOn: (element) => element.on,
    retentionDaysFromSelectValue: () => 365,
    showToast: () => {},
    liveJson: async (url) => {
      assert.equal(authenticated, true);
      if (url === "/api/settings/system") authenticated = false;
    },
    saveWidgetSettings: async () => {
      assert.equal(authenticated, true);
      widgetsSaved = true;
    },
    refreshLiveState: async () => { throw new Error("login required"); },
  };
  vm.runInNewContext(source.slice(0, end), context);
  await save();
  assert.equal(widgetsSaved, true, "pending display settings survive reauthentication");
  assert.equal(authenticated, false);
}

async function main() {
  await testExpiredSessionRedirect();
  await testPendingWidgetsSavedBeforePasswordChange();
  console.log("session redirect and settings save checks passed");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
