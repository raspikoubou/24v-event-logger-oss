"use strict";

const assert = require("node:assert/strict");
const { createUpdatePanelController } = require("../src/event_logger/web/static/update_panel.js");

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
  console.log("update retry checks passed");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
