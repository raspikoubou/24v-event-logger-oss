"use strict";

/**
 * update panel APIをCommonJSまたはbrowser globalへ登録する。
 * @param {Object} root browser global object。documentがある場合はpanelをmountする。
 * @param {Function} factory controller APIを生成する関数。
 * @returns {void} module.exportsまたはrootへAPIを公開する。
 */
(function initializeUpdatePanelModule(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root && root.document) api.mount(root.document, root);
})(typeof globalThis === "object" ? globalThis : this, /**
 * 公開するupdate controller factoryを作る。
 * @returns {Object} createUpdatePanelControllerとmountを公開するAPI。
 */ function createUpdatePanelApi() {
  const BLOCKING_UPDATE_STATES = new Set(["started", "rollback_failed"]);

  /**
   * 更新確認、適用、結果監視を管理するpanel controllerを構築する。
   * @param {Object} options - Release確認/適用、利用者確認、status読取と時刻供給callback群。
   * @param {Function} options.checkRelease - release確認関数。
   * @param {Function} options.applyRelease - release適用関数。
   * @param {Function} options.confirmApply - 利用者確認関数。
   * @param {Function} options.onChange - state変更callback。
   * @param {Function|null} options.readStatus - local update statusを取得するasync callback。
   * @param {Function} options.now - 現在時刻供給関数。
   * @returns {Object} check/apply/monitor/getStateを公開するcontroller。内部state snapshotはcopyで返す。
   */
  function createUpdatePanelController({
    checkRelease,
    applyRelease,
    confirmApply,
    onChange = () => {},
    readStatus = null,
    now = () => typeof performance === "object" ? performance.now() : Date.now(),
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
      releaseToken: null,
      status: null,
      notes: "",
      message: "更新確認はまだ実行していません。",
    };

    let statusRequestRunning = false;
    let statusRequestEpoch = 0;
    let watchStartedAt = null;
    let previousStatusKey = null;

    /**
     * update statusを表示用分類keyへ正規化する。
     * @param {Object} status - 更新status。
     * @returns {string|null} attempt ID、status、version、message、updated_atをJSON化した比較key。statusなしはnull。
     */
    function statusKey(status) {
      if (!status) return null;
      return JSON.stringify([status.attempt_id, status.status, status.version,
        status.message, status.updated_at]);
    }

    /**
     * 現在のupdate stateのcopyを返す。
     * @returns {Object} update stateのshallow copy。呼出元の変更はcontroller内部stateへ反映されない。
     */
    function snapshot() {
      return { ...state };
    }

    /**
     * update stateを更新し、購読者へ変更を通知する。
     * @param {Object} changes - 更新するstate field群。
     * @returns {void} changesを内部stateへ反映し、更新後snapshotをonChangeへ通知する。
     */
    function updateState(changes) {
      Object.assign(state, changes);
      onChange(snapshot());
    }

    /**
     * Release候補を確認し、利用者表示stateを更新する。
     * @returns {Promise<Object>} Release確認後のstate snapshot。通信・schema失敗はerror message付きstateに反映してsnapshotを返す。
     */
    async function check() {
      if (state.checking || state.confirming || state.applying || state.processing) return snapshot();
      updateState({
        checking: true,
        available: false,
        latestVersion: "確認中…",
        notes: "",
        releaseToken: null,
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
          releaseToken: typeof release.release_token === "string" ? release.release_token : null,
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

    /**
     * 確認済みReleaseの適用を要求して結果監視を開始する。
     * @returns {Promise<Object>} 適用確認・依頼後のstate snapshot。利用者取消、開始失敗、受付後の状態不明もstateとして返す。
     */
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
      // 起動前に走っていた状態取得が、今回の依頼後へ遅れて反映されないようにする。
      statusRequestEpoch += 1;
      statusRequestRunning = false;
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
        const result = await applyRelease({
          expected_version: state.latestVersion, release_token: state.releaseToken,
        });
        if (!result || (result.started !== true && result.outcome_unknown !== true)) {
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
          message: result.outcome_unknown === true
            ? "処理中：更新依頼の応答を確認できません。実際の状態を確認しています。"
            : "処理中：更新しています。完了まで電源を切らないでください。",
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

    /**
     * 更新結果待ちの監視期限を処理する。
     * @returns {boolean} 処理中で監視開始から10分超過ならunknown stateへ移しtrue。期限未到達または監視外ならfalse。
     */
    function expireWatch() {
      if (!state.processing || watchStartedAt === null || now() - watchStartedAt <= 10 * 60 * 1000) return false;
      statusRequestEpoch += 1;
      statusRequestRunning = false;
      watchStartedAt = null;
      updateState({ processing: false, available: false, status: { status: "unknown" },
        message: "更新結果を確認できません。成功・失敗は未確認です。再適用せず、接続と記録状態を確認してください。" });
      return true;
    }

    /**
     * 更新状態を一定間隔で確認し、終端状態まで監視する。
     * @returns {Promise<Object>} local update statusを反映したstate snapshot。成功/失敗/不明をstatusとattempt IDで区別する。
     */
    async function monitor() {
      // 状態取得が応答しない場合も、次回の定期確認で監視期限を判定する。
      if (expireWatch()) return snapshot();
      if (!readStatus || statusRequestRunning || state.applying) return snapshot();
      statusRequestRunning = true;
      const requestEpoch = statusRequestEpoch;
      try {
        const result = await readStatus();
        if (requestEpoch !== statusRequestEpoch || expireWatch()) return snapshot();
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
        if (requestEpoch !== statusRequestEpoch || expireWatch()) return snapshot();
        if (state.processing) {
          // Web停止中の通信失敗は更新失敗ではない。再接続して実際の結果を待つ。
          updateState({ message: "処理中：一時的に接続できません。再接続を待っています。" });
        } else {
          updateState({ message: "更新状態を確認できません。接続を確認してください。" });
        }
      } finally {
        if (requestEpoch === statusRequestEpoch) statusRequestRunning = false;
      }
      expireWatch();
      return snapshot();
    }

    return { check, apply, monitor, getState: snapshot };
  }

  /**
   * update statusを利用者向けlabelへ変換する。
   * @param {Object} status - 更新status。
   * @returns {string} started/completed/failed/rollback_failed/unknownを日本語status labelへ変換する。
   */
  function formatUpdateStatus(status) {
    if (!status) return "更新状態の記録はありません。";
    const label = { started: "処理中", completed: "完了", failed: "失敗",
      rollback_failed: "失敗（復旧にも失敗）", unknown: "結果未確認" }[status.status] || "状態未確認";
    return status.version ? `${label} — ${status.version}` : label;
  }

  /**
   * 現在形式と旧形式の状態を安全に読むreaderを作る。
   * @param {Object} options - local/legacy status reader、適用中判定、単調時計callback群。
   * @param {Function} options.readLocal - local status reader。
   * @param {Function} options.readLegacy - legacy status reader。
   * @param {Function} options.isProcessing - 処理中判定関数。
   * @param {Function} options.now - 現在時刻供給関数。
   * @returns {Function} local statusを優先し、404かつ更新処理中なら20秒経過後一度だけlegacy statusを読むasync function。
   */
  function createLegacyAwareStatusReader({ readLocal, readLegacy, isProcessing, now = () => Date.now() }) {
    let unavailableSince = null;
    let legacyRead = false;
    /**
     * local update statusを読み、旧endpointへ切り替える必要がある場合だけlegacy statusを読む。
     * @returns {Promise<Object>} 現在versionとstatusを持つresponse data。旧endpointは404かつ更新中で20秒経過後に一度だけ読む。
     */
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

  /**
   * 更新panelをDOMへ接続し、操作eventと初期表示を設定する。
   * @param {Document} documentRef - document object。
   * @param {Window} windowRef - window object。
   * @returns {Object|null} panelが利用可能ならcontrollerを構築して返す。panel欠落・demo・非liveではnull。
   */
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

    /**
     * update stateをpanelのDOMへ反映する。
     * @param {Object} state - 現在のstate。
     * @returns {void} current/latest version、status、message、notes、button enabled stateをpanel DOMへ反映する。
     */
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
    /**
     * 更新APIへmethod/path/bodyを指定してrequestする。
     * @param {string} method - HTTP method。
     * @param {string} path - API path。
     * @param {Object|null} body - HTTP body。
     * @returns {Promise<Object>} JSON API response data。method別timeout、same-origin credential、CSRF tokenを適用し、失敗はErrorをrejectする。
     */
    async function requestUpdate(method, path = "/api/update", body = null) {
      const headers = { Accept: "application/json" };
      if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
      if (body !== null) headers["Content-Type"] = "application/json";
      const abort = typeof windowRef.AbortController === "function"
        ? new windowRef.AbortController() : null;
      const timer = abort && typeof windowRef.setTimeout === "function"
        ? windowRef.setTimeout(() => abort.abort(), method === "POST" ? 60_000 : 15_000) : null;
      let response;
      let payload;
      try {
        response = await windowRef.fetch(path, {
          method, headers, credentials: "same-origin", cache: "no-store",
          ...(body !== null ? { body: JSON.stringify(body) } : {}),
          ...(abort ? { signal: abort.signal } : {}),
        });
        payload = await response.json();
      } catch (cause) {
        const error = new Error("サーバー応答を確認できませんでした。");
        error.httpStatus = response?.status;
        error.requestRejected = response?.status >= 400 && response?.status < 500;
        throw error;
      } finally {
        if (timer !== null) windowRef.clearTimeout(timer);
      }
      if (!response.ok || payload.ok === false) {
        const error = new Error(payload.error?.message || "サーバーとの通信に失敗しました。");
        // gateway等の5xxは、処理の応答だけを失った可能性を残す。
        error.requestRejected = (response.status >= 400 && response.status < 500)
          || payload.error?.code === "UPDATE_NOT_STARTED"
          || payload.error?.code === "UPDATE_UNAVAILABLE";
        error.httpStatus = response.status;
        throw error;
      }
      return payload.data;
    }

    const controller = createUpdatePanelController({
      checkRelease: /** @returns {Promise<Object>} GitHub release情報とtokenを含む確認結果。 */
        () => requestUpdate("GET"),
      applyRelease: /** @param {Object} body POSTするrelease tokenと確認済みversion。
       * @returns {Promise<Object>} 更新開始受付結果。
       */ (body) => requestUpdate("POST", "/api/update", body),
      confirmApply: /** @param {Object} versions 現行版と適用対象版。
       * @returns {Promise<boolean>} dialogでの利用者確認結果。
       */ ({ currentVersion, targetVersion }) => {
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
      readStatus: /** @returns {Promise<Object>} ローカル更新状態または互換endpointの状態。
       */ () => readStatus(),
    });
    const readStatus = createLegacyAwareStatusReader({
      readLocal: /** @returns {Promise<Object>} ローカル専用更新状態APIの応答。
       */ () => requestUpdate("GET", "/api/update/status"),
      readLegacy: /** @returns {Promise<Object>} 旧更新状態endpointの応答。
       */ () => requestUpdate("GET"),
      isProcessing: /** @returns {boolean} controllerが更新結果待ち状態か。
       */ () => controller.getState().processing,
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
