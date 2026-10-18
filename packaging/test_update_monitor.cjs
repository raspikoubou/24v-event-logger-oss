"use strict";

const assert = require("node:assert/strict");
const { createUpdatePanelController, mount } = require("../src/event_logger/web/static/update_panel.js");

/**
 * 旧完了結果を新attemptと誤認せず、新しいstatus到着後だけ監視を終える。
 * @param {string} previousState 直前attemptのstatus。
 * @param {boolean} withAttemptId fixtureにattempt_idを含めるか。
 * @returns {Promise<void>} retryと状態遷移のassertion完了後に解決する。
 */
async function testRetry(previousState, withAttemptId) {
  let time = 0;
  const previous = {
    status: previousState,
    version: "1.0.2",
    updated_at: "2026-01-01T00:00:00Z",
    ...(withAttemptId ? { attempt_id: "previous" } : {}),
  };
  let result = { current_version: "1.0.1", status: previous };
  let applyCalls = 0;
  const controller = createUpdatePanelController({
    /** @returns {number} 制御可能な経過時刻（ミリ秒）。 */ now: () => time,
    /** @returns {Promise<Object>} 更新可能なreleaseと既存statusのsnapshot。 */ checkRelease: async () => ({
      configured: true, available: true, current_version: "1.0.1",
      latest_version: "1.0.2", status: previous,
    }),
    /** @returns {boolean} 更新確認を承認するmock。 */ confirmApply: () => true,
    /** @returns {Promise<Object>} 新attemptの開始応答。 */ applyRelease: async () => {
      applyCalls += 1;
      return { started: true, target_version: "1.0.2", previous_status: previous,
        ...(withAttemptId ? { attempt_id: "current" } : {}) };
    },
    /** @returns {Promise<Object>} 現在のfixture status。 */ readStatus: async () => result,
  });
  await controller.check();
  await controller.apply();
  await controller.monitor();
  assert.equal(controller.getState().processing, true, "previous result must not finish a retry");
  await controller.apply();
  assert.equal(applyCalls, 1, "retry remains blocked until the new result arrives");
  result = {
    current_version: "1.0.1",
    status: { ...previous, status: "started", ...(withAttemptId ? { attempt_id: "current" } : {}) },
  };
  await controller.monitor();
  assert.equal(controller.getState().processing, true);
  result = {
    current_version: "1.0.2",
    status: { ...previous, status: "completed", ...(withAttemptId
      ? { attempt_id: "current" } : { updated_at: "2026-01-01T00:00:01Z" }) },
  };
  await controller.monitor();
  assert.equal(controller.getState().processing, false);
  assert.match(controller.getState().message, /完了/);
}

/**
 * 秒単位のupdated_atが同じでもattempt_idの変化で新しい失敗を識別する。
 * @returns {Promise<void>} failure状態のassertion完了後に解決する。
 */
async function testNewFailureInSameSecond() {
  const old = { status: "failed", version: "1.0.2", updated_at: "same-second", attempt_id: "old" };
  const controller = createUpdatePanelController({
    /** @returns {Promise<Object>} 古い失敗履歴を持つ更新確認snapshot。 */ checkRelease: async () => ({ configured: true, available: true, current_version: "1.0.1", latest_version: "1.0.2", status: old }),
    /** @returns {boolean} 更新確認を承認するmock。 */ confirmApply: () => true,
    /** @returns {Promise<Object>} 新attempt IDで開始した応答。 */ applyRelease: async () => ({ started: true, target_version: "1.0.2", previous_status: old, attempt_id: "new" }),
    /** @returns {Promise<Object>} 同秒の新attempt failure status。 */ readStatus: async () => ({ current_version: "1.0.1", status: { ...old, attempt_id: "new" } }),
  });
  await controller.check();
  await controller.apply();
  await controller.monitor();
  assert.equal(controller.getState().processing, false);
  assert.match(controller.getState().message, /失敗/);
}

/**
 * check後からapply直前までに更新された履歴snapshotをapply応答が保持する。
 * @returns {Promise<void>} 期限切れstatusがunknownになることを確認後に解決する。
 */
async function testSnapshotTakenImmediatelyBeforeLaunch() {
  const prior = { status: "failed", version: "1.0.2", attempt_id: "between-check-and-apply" };
  let time = 0;
  const controller = createUpdatePanelController({
    /** @returns {number} テストから進める単調時刻（ミリ秒）。 */ now: () => time,
    /** @returns {Promise<Object>} statusなしの更新確認snapshot。 */ checkRelease: async () => ({ configured: true, available: true, current_version: "1.0.1", latest_version: "1.0.2", status: null }),
    /** @returns {boolean} 更新確認を承認するmock。 */ confirmApply: () => true,
    /** @returns {Promise<Object>} apply直前に存在したpriorを返す開始応答。 */ applyRelease: async () => ({ started: true, target_version: "1.0.2", previous_status: prior, attempt_id: "requested" }),
    /** @returns {Promise<Object>} priorのまま変化していないstatus。 */ readStatus: async () => ({ current_version: "1.0.1", status: prior }),
  });
  await controller.check();
  await controller.apply();
  await controller.monitor();
  assert.equal(controller.getState().processing, true);
  time = 600001;
  await controller.monitor();
  assert.equal(controller.getState().status.status, "unknown");
}

/**
 * 旧POST応答にprevious_statusがなくても既存statusを新結果と誤認しない。
 * @returns {Promise<void>} monitorが処理中を維持するassertion後に解決する。
 */
async function testLegacyPostWithoutPreviousStatus() {
  const prior = { status: "failed", version: "1.0.2", updated_at: "old" };
  const controller = createUpdatePanelController({
    /** @returns {Promise<Object>} 更新確認時のprior status。 */ checkRelease: async () => ({ configured: true, available: true, current_version: "1.0.1", latest_version: "1.0.2", status: prior }),
    /** @returns {boolean} 更新確認を承認するmock。 */ confirmApply: () => true,
    /** @returns {Promise<Object>} legacy形式でprevious_statusを省いた開始応答。 */ applyRelease: async () => ({ started: true, target_version: "1.0.2" }),
    /** @returns {Promise<Object>} 更新前のままのstatus。 */ readStatus: async () => ({ current_version: "1.0.1", status: prior }),
  });
  await controller.check();
  await controller.apply();
  await controller.monitor();
  assert.equal(controller.getState().processing, true);
}

/**
 * updaterのretry識別、履歴snapshot、timeout、応答不確定、通信失敗の各検証を実行する。
 * @returns {Promise<void>} 全検証成功時に解決し、失敗を呼出し元へ返す。
 */
async function main() {
  for (const state of ["completed", "failed"]) {
    for (const withAttemptId of [false, true]) await testRetry(state, withAttemptId);
  }
  await testNewFailureInSameSecond();
  await testSnapshotTakenImmediatelyBeforeLaunch();
  await testLegacyPostWithoutPreviousStatus();
  await testPendingStatusExpires();
  await testUncertainAcknowledgement();
  await testMountedTransportFailures();
  console.log("update retry checks passed");
}

/**
 * status GETが期限後に完了した場合、新しいunknown状態を古い応答で上書きしない。
 * @returns {Promise<void>} timeout後の状態保持を確認して解決する。
 */
async function testPendingStatusExpires() {
  let time = 0;
  let finishStatus;
  const controller = createUpdatePanelController({
    /** @returns {number} テスト制御の単調時刻（ミリ秒）。 */ now: () => time,
    /** @returns {Promise<Object>} token付きrelease確認snapshot。 */ checkRelease: async () => ({ configured: true, available: true,
      current_version: "1.0.1", latest_version: "1.0.2", release_token: "confirmed" }),
    /** @returns {boolean} 更新確認を承認するmock。 */ confirmApply: () => true,
    /** @param {Object} body apply request body。 @returns {Promise<Object>} tokenを確認した開始応答。 */ applyRelease: async (body) => {
      assert.deepEqual(body, { expected_version: "1.0.2", release_token: "confirmed" });
      return { started: true, target_version: "1.0.2", attempt_id: "current" };
    },
    /** @returns {Promise<Object>} 外部fixtureから完了させるstatus要求。 */ readStatus: () => new Promise((resolve) => { finishStatus = resolve; }),
  });
  await controller.check(); await controller.apply();
  const pending = controller.monitor();
  time = 600001;
  await controller.monitor();
  assert.equal(controller.getState().processing, false);
  assert.equal(controller.getState().status.status, "unknown");
  finishStatus({ current_version: "1.0.2",
    status: { status: "completed", version: "1.0.2", attempt_id: "current" } });
  await pending;
  assert.equal(controller.getState().status.status, "unknown", "expired response must not overwrite a newer state");
}

/**
 * 開始結果が不確定でも後続statusで実際の開始を確認し、監視を継続する。
 * @returns {Promise<void>} 最終完了状態のassertion後に解決する。
 */
async function testUncertainAcknowledgement() {
  const prior = { status: "failed", version: "1.0.2", attempt_id: "previous" };
  let status = prior;
  const controller = createUpdatePanelController({
    /** @returns {Promise<Object>} prior失敗を含む更新確認snapshot。 */ checkRelease: async () => ({ configured: true, available: true,
      current_version: "1.0.1", latest_version: "1.0.2", status: prior }),
    /** @returns {boolean} 更新確認を承認するmock。 */ confirmApply: () => true,
    /** @returns {Promise<Object>} request結果が不明であることを示す応答。 */ applyRelease: async () => ({ started: false, outcome_unknown: true,
      target_version: "1.0.2", previous_status: prior }),
    /** @returns {Promise<Object>} 現在のstatus fixture。 */ readStatus: async () => ({ current_version: "1.0.1", status }),
  });
  await controller.check(); await controller.apply(); await controller.monitor();
  assert.equal(controller.getState().processing, true);
  status = { status: "completed", version: "1.0.2", attempt_id: "actually-started" };
  await controller.monitor();
  assert.equal(controller.getState().processing, false);
  assert.match(controller.getState().message, /完了/);
}

/**
 * mount経由のHTTP失敗、POST timeout、AbortController cleanupをFake DOM/clockで検証する。
 * @returns {Promise<void>} 通信ごとのcontroller stateを検証後に解決する。
 */
async function testMountedTransportFailures() {
  for (const [httpStatus, code, processing] of [[502, "UPDATE_FAILED", true], [409, "UPDATE_TARGET_CHANGED", false], [502, "UPDATE_NOT_STARTED", false]]) {
    const elements = new Map(["update-panel", "update-check", "update-apply",
      "update-current-version", "update-latest-version", "update-status", "update-message",
      "update-notes", "update-progress"].map((id) => [id, { /** @param {string} _event event名。 @param {Function} _handler mockでは登録を保持しない。 @returns {void} DOM listener登録を受け付ける。 */ addEventListener() {} }]));
    const documentRef = { /** @param {string} id element id。 @returns {Object|null} map上のelement、なければnull。 */ getElementById: (id) => elements.get(id) || null,
      /** @returns {{content: string}} CSRF tokenを持つmeta要素。 */ querySelector: () => ({ content: "csrf" }) };
    const controller = mount(documentRef, { EVENT_LOGGER_LIVE: true, /** @returns {boolean} 確認dialogを承認する。 */ confirm: () => true,
      /** @param {string} url request URL。 @param {Object} options HTTP method等。 @returns {Promise<Object>} methodとfixture codeに応じた応答。 */ fetch: async (url, options) => ({ ok: options.method === "GET", status: options.method === "GET" ? 200 : httpStatus,
        /** @returns {Promise<Object>} GET成功snapshotまたはPOST失敗body。 */ json: async () => options.method === "GET" ? { ok: true, data: {
          configured: true, available: true, current_version: "1.0.1", latest_version: "1.0.2",
          release_token: "confirmed", status: null,
        } } : { ok: false, error: { code, message: "test transport" } },
      }),
    });
    await controller.check(); await controller.apply();
    assert.equal(controller.getState().processing, processing, `${httpStatus}/${code}`);
  }
  const timers = new Map();
  let timerId = 0;
  const elements = new Map(["update-panel", "update-check", "update-apply",
    "update-current-version", "update-latest-version", "update-status", "update-message",
    "update-notes"].map((id) => [id, { /** @param {string} _event event名。 @param {Function} _handler mockでは登録を保持しない。 @returns {void} DOM listener登録を受け付ける。 */ addEventListener() {} }]));
  const controller = mount({ /** @param {string} id element id。 @returns {Object|null} map上のelement、なければnull。 */ getElementById: (id) => elements.get(id) || null,
    /** @returns {{content: string}} CSRF tokenを持つmeta要素。 */ querySelector: () => ({ content: "csrf" }) }, {
    EVENT_LOGGER_LIVE: true, /** @returns {boolean} 確認dialogを承認する。 */ confirm: () => true, AbortController,
    /** @param {Function} fn timeout callback。 @param {number} ms delay（ミリ秒）。 @returns {number} timer id。期限付きtimerをmapへ登録する。 */ setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    /** @param {number} id timer id。 @returns {boolean} mapから削除したか。 */ clearTimeout: (id) => timers.delete(id),
    /** @param {string} _url request URL。 @param {Object} options HTTP methodとabort signal。 @returns {Promise<Object>} GET snapshotまたはabort可能なPOST応答。 */ fetch: async (_url, options) => {
      if (options.method === "POST") {
        assert.deepEqual(JSON.parse(options.body), { expected_version: "1.0.2", release_token: "confirmed" });
        return new Promise((_resolve, reject) => options.signal.addEventListener("abort", /** @returns {void} abort時にrequest promiseをrejectする。 */ () => reject(new Error("aborted"))));
      }
      return { ok: true, status: 200, /** @returns {Promise<Object>} 成功snapshotを含むJSON応答。 */ json: async () => ({ ok: true, data: {
        configured: true, available: true, current_version: "1.0.1", latest_version: "1.0.2",
        release_token: "confirmed", status: null,
      } }) };
    },
  });
  await controller.check();
  const pending = controller.apply();
  await Promise.resolve();
  const postTimer = [...timers.values()].find((timer) => timer.ms === 60_000);
  assert(postTimer, "POST has a bounded wait without using real time in the test");
  postTimer.fn();
  await pending;
  assert.equal(controller.getState().processing, true);
  assert.match(controller.getState().message, /応答を確認できません/);
  assert.equal(timers.size, 0);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
