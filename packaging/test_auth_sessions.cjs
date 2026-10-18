"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const parts = path.join(__dirname, "../src/event_logger/web/frontend/parts");

/**
 * ログイン切れのJSON API要求がログイン画面へ遷移し、正常応答ではdataを返すことを確認する。
 * @returns {Promise<void>} 401時の遷移先と200時の応答内容の検証後に解決する。
 */
async function testExpiredSessionRedirect() {
  const source = fs.readFileSync(path.join(parts, "00_bootstrap.jsfrag"), "utf8");
  const start = source.indexOf("  async function liveJson(");
  const end = source.indexOf("  const { dashboardApiUrl", start);
  const destinations = [];
  const context = {
    document: { /** ログイン画面に対象要素がない状態を返す。 @returns {null} 対象要素なしを示す。 */ querySelector: () => null },
    window: { location: { /** @param {string} url 遷移先URL。 @returns {number} 配列への追加後の長さ。遷移を記録する。 */ assign: (url) => destinations.push(url) } },
    fetch: async () => ({ status: 401, ok: false }),
  };
  vm.runInNewContext(source.slice(start, end) + "\nthis.request = liveJson;", context);
  await assert.rejects(context.request("/api/status"), /ログインし直して/);
  assert.deepEqual(destinations, ["/login"]);
  context.fetch = async () => ({ status: 200, ok: true, /** 成功応答のJSONを返す。 @returns {Promise<{data: string}>} APIのdataを含むmock応答。 */ json: async () => ({ data: "accepted" }) });
  assert.equal(await context.request("/api/status"), "accepted");
  assert.equal(destinations.length, 1);
}

/**
 * パスワード変更で再認証が必要になっても、先行するwidget設定保存が完了することを検証する。
 * @returns {Promise<void>} 保存順序と認証状態のassertion完了後に解決する。
 */
async function testPendingWidgetsSavedBeforePasswordChange() {
  const source = fs.readFileSync(path.join(parts, "08_system_events.jsfrag"), "utf8");
  const end = source.indexOf('  document.querySelectorAll("[data-settings-target]")');
  let save;
  let authenticated = true;
  let widgetsSaved = false;
  const elements = {
    "#save-settings": { /** @param {string} _event event名。 @param {Function} handler 保存処理。 @returns {void} handlerを呼出し用変数へ保存する。 */ addEventListener: (_event, handler) => { save = handler; } },
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
    document: { /** @param {string} selector 要素検索selector。 @returns {Object|undefined} 対応するmock要素。 */ querySelector: (selector) => elements[selector], /** @returns {Array} 対象要素なしを表す空配列。 */ querySelectorAll: () => [] },
    fixtures: { outputRules: [], settings: { passwordProtection: true } },
    /** @param {Object} element switch要素。 @returns {boolean} mock要素のON状態。 */ switchIsOn: (element) => element.on,
    /** @returns {number} 固定fixtureの保持日数365。 */ retentionDaysFromSelectValue: () => 365,
    /** @returns {void} toast表示を省略する。 */ showToast: () => {},
    /** @param {string} url 設定APIのpath。 @returns {Promise<void>} 認証状態を確認しsystem設定時に失効させる。 */ liveJson: async (url) => {
      assert.equal(authenticated, true);
      if (url === "/api/settings/system") authenticated = false;
    },
    /** @returns {Promise<void>} widget保存の認証状態を確認して保存済みflagを更新する。 */ saveWidgetSettings: async () => {
      assert.equal(authenticated, true);
      widgetsSaved = true;
    },
    /** @returns {Promise<never>} 再読込失敗を表す認証例外でrejectする。 */ refreshLiveState: async () => { throw new Error("login required"); },
  };
  vm.runInNewContext(source.slice(0, end), context);
  await save();
  assert.equal(widgetsSaved, true, "pending display settings survive reauthentication");
  assert.equal(authenticated, false);
}

/**
 * 認証切れの遷移とwidget設定保存順の2ケースを順に実行する。
 * @returns {Promise<void>} 全検証が成功した場合に解決し、失敗時は例外を伝播する。
 */
async function main() {
  await testExpiredSessionRedirect();
  await testPendingWidgetsSavedBeforePasswordChange();
  console.log("session redirect and settings save checks passed");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
