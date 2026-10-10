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
  }) {
    const state = {
      checking: false,
      confirming: false,
      applying: false,
      configured: false,
      available: false,
      currentVersion: "未確認",
      latestVersion: "未確認",
      status: null,
      notes: "",
      message: "更新確認はまだ実行していません。",
    };

    function snapshot() {
      return { ...state };
    }

    function updateState(changes) {
      Object.assign(state, changes);
      onChange(snapshot());
    }

    async function check() {
      if (state.checking || state.confirming || state.applying) return snapshot();
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
      if (!state.available || state.checking || state.confirming || state.applying) {
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

      updateState({
        confirming: false,
        applying: true,
        available: false,
        message: "更新を依頼しています。処理中は画面の再読み込みを避けてください。",
      });
      try {
        const result = await applyRelease();
        if (!result || result.started !== true) {
          throw new Error("更新処理を開始できませんでした。");
        }
        updateState({
          status: result.status && typeof result.status === "object"
            ? result.status
            : { status: "started", version: result.target_version },
          message: `${result.target_version || state.latestVersion} の更新を開始しました。完了後に画面を再読み込みしてください。`,
        });
      } catch (error) {
        updateState({
          message: `更新を開始できませんでした。再確認してから操作してください。${error instanceof Error ? ` ${error.message}` : ""}`,
        });
      } finally {
        updateState({ applying: false });
      }
      return snapshot();
    }

    return { check, apply, getState: snapshot };
  }

  function formatUpdateStatus(status) {
    if (!status) return "更新状態の記録はありません。";
    const values = [status.status, status.version, status.message, status.updated_at]
      .filter((value) => typeof value === "string" && value.length > 0);
    return values.length ? values.join(" — ") : "更新状態の形式を確認できません。";
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
      elements.check.disabled = state.checking || state.confirming || state.applying;
      elements.check.textContent = state.checking ? "確認中…" : "更新を確認";
      elements.apply.disabled = !state.available || state.checking || state.confirming || state.applying;
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
    async function requestUpdate(method) {
      const headers = { Accept: "application/json" };
      if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
      const response = await windowRef.fetch("/api/update", {
        method,
        headers,
        credentials: "same-origin",
      });
      let payload;
      try {
        payload = await response.json();
      } catch {
        throw new Error("サーバー応答を読み取れませんでした。");
      }
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error?.message || "サーバーとの通信に失敗しました。");
      }
      return payload.data;
    }

    const controller = createUpdatePanelController({
      checkRelease: () => requestUpdate("GET"),
      applyRelease: () => requestUpdate("POST"),
      confirmApply: ({ currentVersion, targetVersion }) => windowRef.confirm(
        `設備イベントロガーを ${currentVersion} から ${targetVersion} へ更新します。\n\n`
        + "更新中は関連serviceが停止し、記録されない区間はUNKNOWNになります。"
        + "適用前にDB backupを取得し、失敗時は旧版へ戻します。続けますか？",
      ),
      onChange: render,
    });
    elements.check.addEventListener("click", () => controller.check());
    elements.apply.addEventListener("click", () => controller.apply());
    render(controller.getState());
    return controller;
  }

  return { createUpdatePanelController, formatUpdateStatus, mount };
});
