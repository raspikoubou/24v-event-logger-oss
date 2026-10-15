"use strict";

const assert = require("node:assert/strict");
const { createUpdatePanelController, mount } = require("../src/event_logger/web/static/update_panel.js");

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
    now: () => time,
    checkRelease: async () => ({
      configured: true, available: true, current_version: "1.0.1",
      latest_version: "1.0.2", status: previous,
    }),
    confirmApply: () => true,
    applyRelease: async () => {
      applyCalls += 1;
      return { started: true, target_version: "1.0.2", previous_status: previous,
        ...(withAttemptId ? { attempt_id: "current" } : {}) };
    },
    readStatus: async () => result,
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

async function testNewFailureInSameSecond() {
  const old = { status: "failed", version: "1.0.2", updated_at: "same-second", attempt_id: "old" };
  const controller = createUpdatePanelController({
    checkRelease: async () => ({ configured: true, available: true, current_version: "1.0.1", latest_version: "1.0.2", status: old }),
    confirmApply: () => true,
    applyRelease: async () => ({ started: true, target_version: "1.0.2", previous_status: old, attempt_id: "new" }),
    readStatus: async () => ({ current_version: "1.0.1", status: { ...old, attempt_id: "new" } }),
  });
  await controller.check();
  await controller.apply();
  await controller.monitor();
  assert.equal(controller.getState().processing, false);
  assert.match(controller.getState().message, /失敗/);
}

async function testSnapshotTakenImmediatelyBeforeLaunch() {
  const prior = { status: "failed", version: "1.0.2", attempt_id: "between-check-and-apply" };
  let time = 0;
  const controller = createUpdatePanelController({
    now: () => time,
    checkRelease: async () => ({ configured: true, available: true, current_version: "1.0.1", latest_version: "1.0.2", status: null }),
    confirmApply: () => true,
    applyRelease: async () => ({ started: true, target_version: "1.0.2", previous_status: prior, attempt_id: "requested" }),
    readStatus: async () => ({ current_version: "1.0.1", status: prior }),
  });
  await controller.check();
  await controller.apply();
  await controller.monitor();
  assert.equal(controller.getState().processing, true);
  time = 600001;
  await controller.monitor();
  assert.equal(controller.getState().status.status, "unknown");
}

async function testLegacyPostWithoutPreviousStatus() {
  const prior = { status: "failed", version: "1.0.2", updated_at: "old" };
  const controller = createUpdatePanelController({
    checkRelease: async () => ({ configured: true, available: true, current_version: "1.0.1", latest_version: "1.0.2", status: prior }),
    confirmApply: () => true,
    applyRelease: async () => ({ started: true, target_version: "1.0.2" }),
    readStatus: async () => ({ current_version: "1.0.1", status: prior }),
  });
  await controller.check();
  await controller.apply();
  await controller.monitor();
  assert.equal(controller.getState().processing, true);
}

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

async function testPendingStatusExpires() {
  let time = 0;
  let finishStatus;
  const controller = createUpdatePanelController({
    now: () => time,
    checkRelease: async () => ({ configured: true, available: true,
      current_version: "1.0.1", latest_version: "1.0.2", release_token: "confirmed" }),
    confirmApply: () => true,
    applyRelease: async (body) => {
      assert.deepEqual(body, { expected_version: "1.0.2", release_token: "confirmed" });
      return { started: true, target_version: "1.0.2", attempt_id: "current" };
    },
    readStatus: () => new Promise((resolve) => { finishStatus = resolve; }),
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

async function testUncertainAcknowledgement() {
  const prior = { status: "failed", version: "1.0.2", attempt_id: "previous" };
  let status = prior;
  const controller = createUpdatePanelController({
    checkRelease: async () => ({ configured: true, available: true,
      current_version: "1.0.1", latest_version: "1.0.2", status: prior }),
    confirmApply: () => true,
    applyRelease: async () => ({ started: false, outcome_unknown: true,
      target_version: "1.0.2", previous_status: prior }),
    readStatus: async () => ({ current_version: "1.0.1", status }),
  });
  await controller.check(); await controller.apply(); await controller.monitor();
  assert.equal(controller.getState().processing, true);
  status = { status: "completed", version: "1.0.2", attempt_id: "actually-started" };
  await controller.monitor();
  assert.equal(controller.getState().processing, false);
  assert.match(controller.getState().message, /完了/);
}

async function testMountedTransportFailures() {
  for (const [httpStatus, code, processing] of [[502, "UPDATE_FAILED", true], [409, "UPDATE_TARGET_CHANGED", false], [502, "UPDATE_NOT_STARTED", false]]) {
    const elements = new Map(["update-panel", "update-check", "update-apply",
      "update-current-version", "update-latest-version", "update-status", "update-message",
      "update-notes", "update-progress"].map((id) => [id, { addEventListener() {} }]));
    const documentRef = { getElementById: (id) => elements.get(id) || null,
      querySelector: () => ({ content: "csrf" }) };
    const controller = mount(documentRef, { EVENT_LOGGER_LIVE: true, confirm: () => true,
      fetch: async (url, options) => ({ ok: options.method === "GET", status: options.method === "GET" ? 200 : httpStatus,
        json: async () => options.method === "GET" ? { ok: true, data: {
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
    "update-notes"].map((id) => [id, { addEventListener() {} }]));
  const controller = mount({ getElementById: (id) => elements.get(id) || null,
    querySelector: () => ({ content: "csrf" }) }, {
    EVENT_LOGGER_LIVE: true, confirm: () => true, AbortController,
    setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => timers.delete(id),
    fetch: async (_url, options) => {
      if (options.method === "POST") {
        assert.deepEqual(JSON.parse(options.body), { expected_version: "1.0.2", release_token: "confirmed" });
        return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted"))));
      }
      return { ok: true, status: 200, json: async () => ({ ok: true, data: {
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
