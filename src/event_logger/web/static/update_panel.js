"use strict";

(function initializeUpdatePanelModule(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root && root.document) api.mount(root.document, root);
})(typeof globalThis === "object" ? globalThis : this, function createUpdatePanelApi() {
  const BLOCKING_UPDATE_STATES = new Set(["started", "rollback_failed"]);

  function createUpdatePanelController({
    checkRelease,
    applyRelease,
    confirmApply,
    onChange = () => {},
    readStatus = null,
    now = () => Date.now(),
  }) {
    const state = {
      checking: false,
      confirming: false,
      applying: false,
      processing: false,
      targetVersion: null,
      targetAttemptId: null,
      configured: false,
      available: false,
      currentVersion: "未確認",
      latestVersion: "未確認",
      status: null,
      notes: "",
      message: "更新確認はまだ実行していません。",
    };

    let statusRequestRunning = false;
    let watchStartedAt = null;
    let previousStatusKey = null;

    function statusKey(status) {
      if (!status) return null;
      return JSON.stringify([status.attempt_id, status.status, status.version,
        status.message, status.updated_at]);
    }

    function snapshot() {
      return { ...state };
    }

    function updateState(changes) {
      Object.assign(state, changes);
      onChange(snapshot());
    }

    async function check() {
      if (state.checking || state.confirming || state.applying || state.processing) return snapshot();
      updateState({
        checking: true,
        available: false,
        latestVersion: "確認中…",
        notes: "",
        message: "安定版Releaseを確認しています。",
      });
      try {
        const release = await checkRelease();
        if (!release || typeof release !== "object") {
          throw new Error("更新確認の応答を読み取れませんでした。");
        }
        if (typeof release.current_version !== "string") {
          throw new Error("現在のVersionを確認できませんでした。");
        }

        const updateStatus = release.status && typeof release.status === "object"
          ? release.status
          : null;
        const blockingStatus = BLOCKING_UPDATE_STATES.has(updateStatus?.status);
        if (release.configured !== true) {
          updateState({
            configured: false,
            currentVersion: release.current_version,
            latestVersion: "未設定",
            status: updateStatus,
            notes: "",
            message: "この環境では更新配布先が設定されていません。",
          });
          return snapshot();
        }
        if (typeof release.latest_version !== "string" || typeof release.available !== "boolean") {
          throw new Error("Release情報の形式が正しくありません。");
        }

        const available = release.available && !blockingStatus;
        const message = blockingStatus
          ? "前回の更新処理が完了していません。再適用せず、更新状態とシステムログを確認してください。"
          : available
            ? "新しい安定版があります。適用すると一時的に記録が止まり、その区間はUNKNOWNになります。"
            : "最新の安定版を使用しています。";
        updateState({
          configured: true,
          available,
          currentVersion: release.current_version,
          latestVersion: release.latest_version,
          status: updateStatus,
          notes: typeof release.notes === "string" ? release.notes : "",
          message,
        });
      } catch (error) {
        updateState({
          configured: false,
          available: false,
          latestVersion: "確認できません",
          notes: "",
          message: `更新を確認できませんでした。${error instanceof Error ? error.message : "通信状態を確認してください。"}`,
        });
      } finally {
        updateState({ checking: false });
      }
      return snapshot();
    }

    async function apply() {
      if (!state.available || state.checking || state.confirming || state.applying || state.processing) {
        return snapshot();
      }
      updateState({ confirming: true, message: "適用前の確認を待っています。" });
      let accepted = false;
      try {
        accepted = await confirmApply({
          currentVersion: state.currentVersion,
          targetVersion: state.latestVersion,
        });
      } catch (error) {
        updateState({
          confirming: false,
          message: `更新の確認を表示できませんでした。${error instanceof Error ? error.message : ""}`,
        });
        return snapshot();
      }
      if (!accepted || !state.available || state.applying) {
        updateState({ confirming: false, message: "更新の適用をキャンセルしました。" });
        return snapshot();
      }

      previousStatusKey = statusKey(state.status);
      updateState({
        confirming: false,
        applying: true,
        processing: true,
        targetVersion: state.latestVersion,
        targetAttemptId: null,
        status: { status: "started", version: state.latestVersion },
        available: false,
        message: "更新を依頼しています。処理中は画面の再読み込みを避けてください。",
      });
      watchStartedAt = now();
      try {
        const result = await applyRelease();
        if (!result || result.started !== true) {
          throw new Error("更新処理を開始できませんでした。");
        }
        if (Object.prototype.hasOwnProperty.call(result, "previous_status")) {
          previousStatusKey = statusKey(result.previous_status);
        }
        updateState({
          // POST応答のstatusは前回の更新記録の場合がある。今回の対象で監視する。
          targetVersion: result.target_version || state.latestVersion,
          targetAttemptId: typeof result.attempt_id === "string" ? result.attempt_id : null,
          status: { status: "started", version: result.target_version || state.latestVersion },
          message: "処理中：更新しています。完了まで電源を切らないでください。",
        });
      } catch (error) {
        updateState({
          processing: error?.requestRejected !== true,
          status: { status: error?.requestRejected === true ? "failed" : "started", version: state.targetVersion },
          message: error?.requestRejected === true
            ? `失敗：更新を開始できませんでした。${error.message}`
            : "処理中：更新依頼の応答を確認できません。実際の状態を確認しています。",
        });
      } finally {
        updateState({ applying: false });
      }
      return snapshot();
    }

    async function monitor() {
      if (!readStatus || statusRequestRunning || state.applying) return snapshot();
      statusRequestRunning = true;
      try {
        const result = await readStatus();
        if (!result || typeof result.current_version !== "string") {
          throw new Error("更新状態の応答を確認できません。");
        }
        updateState({ currentVersion: result.current_version });
        const status = result.status;
        const previousOrOtherAttempt = state.targetAttemptId !== null
          ? status?.attempt_id !== state.targetAttemptId
          : statusKey(status) === previousStatusKey;
        // 同じ版への再試行でも、依頼前の成功・失敗を今回の結果にしない。
        // 旧更新器にattempt_idがない場合は記録全体を比較し、区別できない
        // 結果を成功・失敗と推測せず、既存の未確認timeoutまで監視する。
        if (state.processing && (!status || status.version !== state.targetVersion
          || previousOrOtherAttempt)) {
          updateState({ message: "処理中：更新処理の状態を確認しています。" });
        } else if (status?.status === "started") {
          if (watchStartedAt === null) watchStartedAt = now();
          updateState({ processing: true, available: false, targetVersion: status.version, status,
            message: "処理中：更新しています。完了まで電源を切らないでください。" });
        } else if (status?.status === "completed") {
          updateState({ processing: false, status, message:
            `完了：${status.version || result.current_version} の更新が完了しました。画面を再読み込みしてください。` });
          watchStartedAt = null;
        } else if (status?.status === "failed" || status?.status === "rollback_failed") {
          updateState({ processing: false, available: false, status, message:
            status.status === "rollback_failed"
              ? "失敗：更新後の復旧にも失敗しました。再適用せず、記録状態とエラーを確認してください。"
              : "失敗：更新に失敗し、元の状態へ戻しました。現在のVersionと記録状態を確認してください。" });
          watchStartedAt = null;
        } else if (!state.processing) {
          updateState({ status: null });
        }
      } catch (error) {
        if (state.processing) {
          // Web停止中の通信失敗は更新失敗ではない。再接続して実際の結果を待つ。
          updateState({ message: "処理中：一時的に接続できません。再接続を待っています。" });
        } else {
          updateState({ message: "更新状態を確認できません。接続を確認してください。" });
        }
      } finally {
        statusRequestRunning = false;
      }
      if (state.processing && watchStartedAt !== null && now() - watchStartedAt > 10 * 60 * 1000) {
        // 通信断から成功・失敗を推測しない。長時間結果が取れない場合は明示的に未確認にする。
        updateState({ processing: false, available: false, status: { status: "unknown" },
          message: "更新結果を確認できません。成功・失敗は未確認です。再適用せず、接続と記録状態を確認してください。" });
      }
      return snapshot();
    }

    return { check, apply, monitor, getState: snapshot };
  }

  function formatUpdateStatus(status) {
    if (!status) return "更新状態の記録はありません。";
    const label = { started: "処理中", completed: "完了", failed: "失敗",
      rollback_failed: "失敗（復旧にも失敗）", unknown: "結果未確認" }[status.status] || "状態未確認";
    return status.version ? `${label} — ${status.version}` : label;
  }

  function createLegacyAwareStatusReader({ readLocal, readLegacy, isProcessing, now = () => Date.now() }) {
    let unavailableSince = null;
    let legacyRead = false;
    return async function readStatus() {
      try {
        const result = await readLocal();
        unavailableSince = null;
        legacyRead = false;
        return result;
      } catch (error) {
        if (error?.httpStatus !== 404 || !isProcessing()) throw error;
        if (unavailableSince === null) unavailableSince = now();
        // 配布済み旧版はローカル状態APIを持たない。新Web起動直後のhealth
        // 確認を待ってから、既存endpointで結果を一度だけ確認する。
        // 旧endpointはGitHubにも通信するため、定期取得へ切り替えない。
        if (!legacyRead && now() - unavailableSince >= 20_000) {
          legacyRead = true;
          return readLegacy();
        }
        throw error;
      }
    };
  }

  function mount(documentRef, windowRef) {
    const panel = documentRef.getElementById("update-panel");
    if (!panel) return null;

    const elements = {
      check: documentRef.getElementById("update-check"),
      apply: documentRef.getElementById("update-apply"),
      current: documentRef.getElementById("update-current-version"),
      latest: documentRef.getElementById("update-latest-version"),
      status: documentRef.getElementById("update-status"),
      message: documentRef.getElementById("update-message"),
      notes: documentRef.getElementById("update-notes"),
    };
    if (Object.values(elements).some((element) => !element)) return null;

    function render(state) {
      const progress = documentRef.getElementById("update-progress");
      const processing = state.processing || state.applying;
      if (panel.dataset) panel.dataset.updateProcessing = String(processing);
      if (progress) progress.hidden = !processing;
      elements.check.disabled = state.checking || state.confirming || processing;
      elements.check.textContent = state.checking ? "確認中…" : "更新を確認";
      elements.apply.disabled = !state.available || state.checking || state.confirming || processing;
      elements.apply.textContent = state.applying ? "依頼中…" : "更新を適用";
      elements.current.textContent = state.currentVersion;
      elements.latest.textContent = state.latestVersion;
      elements.status.textContent = formatUpdateStatus(state.status);
      elements.message.textContent = state.message;
      elements.notes.textContent = state.notes;
      elements.notes.hidden = !state.notes;
    }

    if (windowRef.EVENT_LOGGER_DEMO) {
      elements.check.disabled = true;
      elements.apply.disabled = true;
      elements.message.textContent = "デモモードでは更新確認と適用を行いません。";
      return null;
    }
    if (!windowRef.EVENT_LOGGER_LIVE) {
      elements.check.disabled = true;
      elements.apply.disabled = true;
      elements.message.textContent = "表示確認モードでは更新確認を利用できません。";
      return null;
    }

    const csrfToken = documentRef.querySelector('meta[name="csrf-token"]')?.content || "";
    async function requestUpdate(method, path = "/api/update") {
      const headers = { Accept: "application/json" };
      if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
      const response = await windowRef.fetch(path, {
        method,
        headers,
        credentials: "same-origin",
        cache: "no-store",
      });
      let payload;
      try {
        payload = await response.json();
      } catch {
        const error = new Error("サーバー応答を読み取れませんでした。");
        error.httpStatus = response.status;
        error.requestRejected = !response.ok;
        throw error;
      }
      if (!response.ok || payload.ok === false) {
        const error = new Error(payload.error?.message || "サーバーとの通信に失敗しました。");
        error.requestRejected = true;
        error.httpStatus = response.status;
        throw error;
      }
      return payload.data;
    }

    const controller = createUpdatePanelController({
      checkRelease: () => requestUpdate("GET"),
      applyRelease: () => requestUpdate("POST"),
      confirmApply: ({ currentVersion, targetVersion }) => {
        const message = `設備イベントロガーを ${currentVersion} から ${targetVersion} へ更新します。\n\n`
          + "更新中は記録が一時停止し、その区間はUNKNOWNになります。"
          + "更新前にDBバックアップを取得し、失敗時は旧版への復帰を試みます。";
        const dialog = documentRef.getElementById("update-dialog");
        const text = documentRef.getElementById("update-dialog-message");
        if (!dialog || !text) return windowRef.confirm(message);
        text.textContent = message;
        dialog.returnValue = "";
        return new Promise((resolve) => {
          dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
          dialog.showModal();
        });
      },
      onChange: render,
      readStatus: () => readStatus(),
    });
    const readStatus = createLegacyAwareStatusReader({
      readLocal: () => requestUpdate("GET", "/api/update/status"),
      readLegacy: () => requestUpdate("GET"),
      isProcessing: () => controller.getState().processing,
    });
    elements.check.addEventListener("click", () => controller.check());
    elements.apply.addEventListener("click", () => controller.apply());
    render(controller.getState());
    // GitHubの更新確認とは別に、ローカルの処理結果だけを自動で確認する。
    controller.monitor();
    if (typeof windowRef.setInterval === "function") {
      windowRef.setInterval(() => {
        if (controller.getState().processing) controller.monitor();
      }, 3000);
    }
    return controller;
  }

  return { createUpdatePanelController, createLegacyAwareStatusReader, formatUpdateStatus, mount };
});
