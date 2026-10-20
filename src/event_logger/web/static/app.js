(() => {
  "use strict";

  // Sitesは固定fixtureのまま確認でき、Piのテンプレートだけが実機APIを有効化する。
  const liveMode = Boolean(window.EVENT_LOGGER_LIVE);

  const {
    shouldRefreshDashboard,
    shouldDeferTimelineRender,
    shouldDeferTimelineWidgetRender,
    shouldRefreshLogPage
  } = window.EventLoggerRefreshPolicy;

  /**
   * JSON APIを取得し、非成功応答を共通errorとして扱う。
   * @param {string} path - API path。
   * @param {Object} options - HTTP fetch options。method、bodyなどを含む。
   * @returns {Promise<Object>} 成功時はpayload.data。401では/loginへ遷移し、非成功HTTPまたはok=falseではErrorをrejectする。
   */
  async function liveJson(path, options = {}) {
    const requestOptions = { ...options, headers: { ...(options.headers || {}) } };
    if (requestOptions.body && !requestOptions.headers["Content-Type"]) {
      requestOptions.headers["Content-Type"] = "application/json";
    }
    if (requestOptions.method && requestOptions.method !== "GET") {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
      if (csrf) requestOptions.headers["X-CSRF-Token"] = csrf;
    }
    const response = await fetch(path, requestOptions);
    if (response.status === 401) {
      // 他端末からのpassword変更でも、古い画面で操作を続けず再認証へ進む。
      window.location.assign("/login");
      throw new Error("ログインし直してください。");
    }
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error?.message || "サーバーとの通信に失敗しました");
    }
    return payload.data;
  }

  const { dashboardApiUrl, buildLogApiQuery, logValueFilterForWidget } = window.EventLoggerQueryPolicy;

  const {
    retentionDaysFromSelectValue,
    retentionDaysFromStatus
  } = window.EventLoggerRetentionPolicy;
  const { serializeDemoCsv } = window.EventLoggerCsvFormat;

  /**
   * 画面初期化に必要な固定fixtureまたはlive API dataを読み込む。
   * @param {string} period - 期間選択値。
   * @param {boolean} includeDashboard - dashboard responseを含めるか。
   * @param {Object|null} customRange - 任意期間の境界値。
   * @param {boolean} includeEvents - event一覧を取得するか。
   * @returns {Promise<Object>} Dashboard、channel、event等を含む初期fixture/API payload。
   */
  async function loadLiveFixtures(
    period = null,
    includeDashboard = false,
    customRange = null,
    includeEvents = true
  ) {
    if (!liveMode) {
      return {
        ...window.DEMO_FIXTURES,
        nowMs: Date.now(),
        loggerStatus: "logging",
        storage: null,
        storageError: false,
        dashboardValues: null,
        dashboardWarning: null,
        unsynchronizedCount: 3,
        configurationSync: { outOfSync: false, stopped: false, message: "" }
      };
    }
    // statusを先に読み、timezone・状態・更新済みcustom rangeを以降のAPI requestの
    // 基準にする。status取得前に各endpointを並列化すると表示世代が混在する。
    const status = await liveJson("/api/status");
    const storageWarning = storageWarningFor(
      status.storage,
      Boolean(status.storage_error)
    );
    const selectedPeriod = period || status.dashboard_period || "8h";
    const selectedCustomRange = customRange || status.dashboard_custom_range;
    let dashboardReceivedAtMs = null;
    // statusで選択条件を確定してから独立endpointを並列取得する。dashboard/logは
    // 呼出元が必要とした時だけ要求し、不要な大きな履歴payloadを避ける。
    const [widgets, rules, setup, logs, dashboard] = await Promise.all([
      liveJson("/api/widgets"),
      liveJson("/api/output-rules"),
      liveJson("/api/setup"),
      includeEvents ? liveJson("/api/logs?limit=500") : Promise.resolve(null),
      includeDashboard
        ? liveJson(dashboardApiUrl(selectedPeriod, selectedCustomRange)).then((data) => {
          dashboardReceivedAtMs = performance.now();
          return data;
        })
        : Promise.resolve(null)
    ]);
    const nowMs = Number(status.clock?.utc_ms || Date.now());
    const timezone = status.system?.timezone || "Asia/Tokyo";
    const periods = {
      "1h": { label: "直近1時間", durationMs: 3_600_000 },
      "8h": { label: "直近8時間", durationMs: 28_800_000 },
      "24h": { label: "直近24時間", durationMs: 86_400_000 },
      "7d": { label: "直近7日", durationMs: 7 * 86_400_000 },
      "30d": { label: "直近30日", durationMs: 30 * 86_400_000 },
      today: { label: "今日", durationMs: 86_400_000 },
      yesterday: { label: "昨日", durationMs: 86_400_000 },
      this_week: { label: "今週", durationMs: 7 * 86_400_000 },
      custom: { label: "任意期間", durationMs: 14_400_000 }
    };
    if (
      Number.isInteger(dashboard?.from_ms)
      && Number.isInteger(dashboard?.to_ms)
      && periods[selectedPeriod]
    ) {
      periods[selectedPeriod] = {
        ...periods[selectedPeriod],
        startAt: new Date(dashboard.from_ms).toISOString(),
        endAt: new Date(dashboard.to_ms).toISOString(),
        durationMs: dashboard.to_ms - dashboard.from_ms
      };
    }
    // APIのsnake_case payloadを画面のcamelCase fixtureへ一度だけ変換する。
    // 不明なlogger状態をOFFと誤表示しないよう、IN/OUT値はUNKNOWNに保つ。
    const knownLoggerState = ["logging", "maintenance", "critical"].includes(status.logger_status);
    /**
     * channelsの処理を行い、画面状態の該当部分を扱う。
     * @param {Object} options - HTTP fetch options。method、bodyなどを含む。
     * @returns {string} 入力を画面またはAPIで使う形式へ変換した文字列。
     */
    const channels = (status.channels || []).map((channel) => ({
      id: channel.id,
      name: channel.name || `IN${channel.id}`,
      enabled: Boolean(channel.enabled),
      value: knownLoggerState
        ? status.inputs?.[String(channel.id)] ? "ON" : "OFF"
        : "UNKNOWN",
      debounce: channel.debounce_ms,
      onLabel: channel.on_label || "ON",
      offLabel: channel.off_label || "OFF"
    }));
    // fixtureのrule列を画面表示用に変換し、API保存形とは分けて保持する。
    const outputRules = (rules.items || []).map((rule) => ({
      id: rule.id,
      name: rule.name,
      condition: rule.condition,
      channelId: `IN${rule.input_channel}`,
      value: rule.value ? "ON" : "OFF",
      edge: rule.edge === "falling" ? "OFF" : "ON",
      duration: Math.max(1, Math.round(rule.duration_ms / 1000)),
      durationUnit: "seconds",
      outputId: rule.output_channel,
      enabled: Boolean(rule.enabled)
    }));
    const heartbeat = status.heartbeat || { enabled: false, channel_id: null, interval_ms: 5000 };
    const outputs = Array.from({ length: 6 }, (_, index) => {
      const id = index + 1;
      const rule = outputRules.find((item) => item.outputId === id);
      const isHeartbeat = heartbeat.enabled && heartbeat.channel_id === id;
      return {
        id,
        name: isHeartbeat ? "Heartbeat" : rule?.name || "未使用",
        owner: isHeartbeat ? "Heartbeat" : rule ? "条件ルール" : "未使用",
        value: knownLoggerState
          ? status.outputs?.[String(id)] ? "ON" : "OFF"
          : "UNKNOWN"
      };
    });
    const events = logs === null ? null : (logs.items || []).map((event, index) => ({
      id: Number(event.id?.split(":").pop()) || index + 1,
      timestamp: event.timestamp,
      signal: event.signal,
      value: event.value,
      kind: event.kind,
      source: event.source || "",
      synced: Boolean(event.clock_synced)
    }));
    return {
      nowMs,
      dashboardPeriods: periods,
      channels,
      outputs,
      settings: {
        deviceName: status.system?.device_name || "設備名未設定",
        hostname: status.system?.hostname || "",
        timezone,
        passwordProtection: Boolean(status.system?.password_protection),
        setupCompleted: Boolean(setup.completed),
        dashboardPeriod: status.dashboard_period || "8h",
        dashboardCustomRange: status.dashboard_custom_range || null,
        heartbeat: {
          enabled: Boolean(heartbeat.enabled),
          outputId: heartbeat.channel_id || 1,
          intervalSeconds: Math.max(1, Math.round((heartbeat.interval_ms || 5000) / 1000))
        },
        retentionDays: retentionDaysFromStatus(status)
      },
      outputRules,
      outputTestChannel: status.daemon?.output_test_channel ?? null,
      events,
      counterResets: {},
      widgets: widgets.items || [],
      dashboardValues: dashboard?.items || null,
      dashboardReceivedAtMs,
      dashboardWarning: dashboard?.warning || null,
      storage: status.storage || null,
      storageError: Boolean(status.storage_error),
      unsynchronizedCount: Number(status.unsynchronized_event_count || 0),
      writeLatency: status.daemon?.write_latency || null,
      configurationSync: {
        outOfSync: status.configuration_sync?.out_of_sync === true,
        stopped: status.configuration_sync?.stopped === true,
        message: String(status.configuration_sync?.message || "")
      },
      health: [
        { label: "App version", value: String(status.app_version || "--"), state: "ok" },
        {
          label: "logger状態",
          value: status.logger_status === "logging"
            ? "動作中"
            : status.logger_status === "critical"
              ? "異常停止（Critical）"
              : status.logger_status === "maintenance"
                ? "メンテナンス"
                : "状態不明",
          state: status.logger_status === "logging"
            ? "ok"
            : status.logger_status === "critical"
              ? "critical"
              : "warning"
        },
        {
          label: "loggerd設定反映",
          value: status.configuration_sync?.out_of_sync === true ? "要再確認" : "一致",
          state: status.configuration_sync?.out_of_sync === true ? "warning" : "ok"
        },
        { label: "DB使用量", value: formatBytes(status.storage?.db_bytes), state: "ok" },
        { label: "SD総容量", value: formatBytes(status.storage?.total_bytes), state: "ok" },
        { label: "SD使用量", value: formatBytes(status.storage?.used_bytes), state: "ok" },
        { label: "SD空き容量", value: formatBytes(status.storage?.free_bytes), state: "ok" },
        { label: "SD空き率", value: `${Math.round((status.storage?.free_ratio || 0) * 100)}%`, state: storageWarning.severity || (status.storage?.status === "normal" ? "ok" : "warning") },
        { label: "ストレージ状態", value: status.storage?.status === "normal" ? "正常" : String(status.storage?.status || "不明"), state: storageWarning.severity || (status.storage?.status === "normal" ? "ok" : "warning") },
        {
          label: "ログ保存遅延（直近5分）",
          value: status.daemon?.write_latency
            ? `${status.daemon.write_latency.warning ? "警告" : status.daemon.write_latency.severity === "attention" ? "注意" : "正常"} · ${Number(status.daemon.write_latency.delayed_count_5m || 0)}回 · 最大${Number(status.daemon.write_latency.max_delay_ms_5m || 0).toFixed(1)}ms · 最長滞留${Number(status.daemon.write_latency.oldest_pending_ms || 0).toFixed(1)}ms · 未保存${Number(status.daemon.pending_event_count || 0)}件`
            : "--",
          state: status.daemon?.write_latency?.warning ? "warning" : status.daemon?.write_latency?.severity === "attention" ? "attention" : "ok"
        },
        { id: "diagnostic-clock", label: "システム時刻", value: "--", state: "ok" },
        { label: "NTP同期状態", value: status.clock?.synchronized ? "同期済み" : "未同期", state: status.clock?.synchronized ? "ok" : "warning" },
        { label: "Heartbeat状態", value: heartbeat.enabled ? `OUT${heartbeat.channel_id} · ${Math.round((heartbeat.interval_ms || 5000) / 1000)}秒` : "無効", state: heartbeat.enabled ? "ok" : "warning" }
      ],
      loggerStatus: status.logger_status
    };
  }

  const {
    formatBytes,
    storageWarningFor,
    storageWarningMessage
  } = window.EventLoggerStoragePolicy;

  /**
   * 初期データを読み、画面のevent handlerと初回描画を開始する。
   * @returns {Promise<void>} 初期データ取得と初期描画が完了した時にresolveする。取得失敗はrejectする。
   */
  async function start() {
    // 初期fixtureが揃うまではrunAppを呼ばず、nullなwidget/channel配列でevent handlerが
    // 起動しないようにする。取得後は画面instanceを一度だけ作る。
    const fixtures = await loadLiveFixtures(null, liveMode);
    runApp(fixtures);
  }

  /**
   * fixtureを共有状態へ設定し、各画面の初期化と描画を行う。
   * @param {Object} fixtures - 固定fixtureまたは現在の画面状態。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function runApp(fixtures) {
  // 画面固有の一時状態はrunAppのclosure内に置き、poll更新後も入力draftやfocusを
  // 同じ画面instanceで扱う。固定fixtureとlive modeで状態モデルを共有する。
  let currentPeriod = fixtures.settings.dashboardPeriod || "8h";
  let dashboardCustomRangeDraft = null;
  let logging = fixtures.loggerStatus === "logging";
  let pendingAction = null;
  let addingWidgetGroup = "realtime";
  let nextDashboardRefreshAtMs = 0;
  let currentWidgetStep = 1;
  let timelineZoomIndex = 0;
  let pinnedTimelineEvent = null;
  let focusedTimelineEvent = false;
  let activeTimelineDetailEvent = null;
  let draggedWidgetRow = null;
  let draggedWidgetOrigin = null;
  let dragWasDropped = false;
  let toastTimer = null;
  let activeView = "dashboard";
  const logState = window.EventLoggerUiState.createLogController();
  const widgetEditor = window.EventLoggerUiState.createWidgetEditorState();
  let activeOutputTest = liveMode && fixtures.outputTestChannel
    ? { outputId: fixtures.outputTestChannel, timerId: null }
    : null;
  let editingOutputRuleId = null;
  let widgetLogSignals = null;
  let loggingStoppedAtMs = null;
  let loggingStartedAtMs = fixtures.nowMs;
  const unknownIntervals = [];
  const timelineZoomLevels = [1, 1.5, 2];
  const demoClockBaseMs = fixtures.nowMs;
  const demoClockStartedAtMs = performance.now();
  const widgetTypeHelp = {
    "現在状態": "指定入力の現在値を表示します。",
    "イベント回数": "指定イベントの回数を表示します。",
    "ON/OFF時間": "指定期間のON時間またはOFF時間を合計します。",
    "最終イベント": "最後に指定イベントが発生した時刻を表示します。",
    "経過時間": "指定状態またはイベントを起点に、現在までの経過時間を表示します。",
    "タイムライン": "IN/OUTの状態履歴を時間軸で表示します。",
    "イベント一覧": "ダッシュボード内に簡易ログを表示します。"
  };

  /**
   * demo画面の時刻軸に使う固定基準時刻を返す。
   * @returns {number} demo fixtureの固定時刻をUTC Unix epoch millisecondで返す。
   */
  function demoNowMs() {
    return demoClockBaseMs + performance.now() - demoClockStartedAtMs;
  }

  const liveState = window.EventLoggerUiState.createLiveStateController(refreshLiveStateOnce);
  const refreshLiveState = liveState.refresh;

  /**
   * 一回分のlive状態を取得し、表示と次回更新時刻を更新する。
   * @param {boolean} forceDashboard - dashboard更新を強制するか。
   * @returns {Promise<void>} live statusと必要なDashboard dataをDOMへ反映後resolveする。通信失敗時はErrorをrejectする。
   */
  async function refreshLiveStateOnce(forceDashboard = false) {
    // API取得前にdashboard更新の要否を判定し、表示周期を別に制御する。
    // status/logのpollで重いdashboard集計まで毎回再取得しない。
    const nowMs = Date.now();
    const customRangeForRefresh = (
      dashboardCustomRangeDraft || fixtures.settings.dashboardCustomRange
    );
    const includeDashboard = shouldRefreshDashboard({
      currentPeriod,
      customRange: customRangeForRefresh,
      liveMode,
      forceDashboard,
      dashboardLoaded: Array.isArray(fixtures.dashboardValues) || Boolean(fixtures.dashboardWarning),
      hasCurrentStateWidgets: fixtures.widgets?.some((widget) => widget.type === "current_state" && widget.enabled !== false),
      nextRefreshAtMs: nextDashboardRefreshAtMs,
      nowMs
    });
    if (includeDashboard) nextDashboardRefreshAtMs = nowMs + 5_000;
    const next = await loadLiveFixtures(
      currentPeriod,
      includeDashboard,
      customRangeForRefresh,
      activeView !== "logs"
    );
    // UI描画中にログ取得が失敗しても、取得済みserver snapshotを失わない。
    liveState.acceptSnapshot(next);
    // 取得したsnapshotをまとめて置き換えてから描画する。custom範囲draftだけは
    // statusの保存値に上書きさせず、「適用」前の画面入力を保持する。
    fixtures.nowMs = next.nowMs;
    fixtures.channels = next.channels;
    fixtures.outputs = next.outputs;
    fixtures.settings = next.settings;
    if (currentPeriod === "custom" && dashboardCustomRangeDraft) {
      // 「適用」前の入力値は画面内だけのdraftとして使う。
      // statusの保存済み値で上書きすると、次のpollが不正なcustom queryへ戻る。
      fixtures.settings.dashboardCustomRange = dashboardCustomRangeDraft;
    }
    fixtures.outputRules = next.outputRules;
    fixtures.outputTestChannel = next.outputTestChannel;
    if (Array.isArray(next.events)) fixtures.events = next.events;
    fixtures.health = next.health;
    fixtures.loggerStatus = next.loggerStatus;
    fixtures.unsynchronizedCount = next.unsynchronizedCount;
    fixtures.writeLatency = next.writeLatency;
    fixtures.configurationSync = next.configurationSync;
    fixtures.storage = next.storage;
    fixtures.storageError = next.storageError;
    fixtures.dashboardWarning = next.dashboardWarning || null;
    if (Array.isArray(next.dashboardValues)) {
      fixtures.dashboardValues = next.dashboardValues;
      fixtures.dashboardReceivedAtMs = next.dashboardReceivedAtMs;
      const bounds = next.dashboardPeriods[currentPeriod];
      if (bounds) fixtures.dashboardPeriods[currentPeriod] = bounds;
    }
    if (next.dashboardWarning) nextDashboardRefreshAtMs = Date.now() + 30_000;
    // output testのtimerは一時状態を追跡する。daemon側でOFFになった場合もtimerを
    // 残さず、別のOUTへ切り替わった場合は以前の表示を終了してから同期する。
    if (liveMode) {
      if (next.outputTestChannel === null) {
        if (activeOutputTest?.timerId) window.clearTimeout(activeOutputTest.timerId);
        activeOutputTest = null;
      } else if (activeOutputTest?.outputId !== next.outputTestChannel) {
        if (activeOutputTest?.timerId) window.clearTimeout(activeOutputTest.timerId);
        activeOutputTest = { outputId: next.outputTestChannel, timerId: null };
      }
    }
    logging = next.loggerStatus === "logging";
    syncLiveControls();
    document.querySelector("#page-title").textContent = next.settings.deviceName;
    renderUnsynchronizedWarnings();
    updateOutputOwners();
    const widgetDialogOpen = document.querySelector("#widget-dialog").open;
    const nextWidgets = next.widgets || [];
    if (
      liveMode
      && widgetEditor.shouldApplyServerWidgets(widgetDialogOpen)
      && JSON.stringify(fixtures.widgets || []) !== JSON.stringify(nextWidgets)
    ) {
      fixtures.widgets = nextWidgets;
      renderPersistedWidgets(nextWidgets);
    }
    // poll stateを先にDOMへ反映し、ログだけは現在viewと5秒期限に従って別取得する。
    // channel編集やwidget modalの入力中はそのフォームを後続描画で壊さない。
    renderDataViews();
    if (
      shouldRefreshLogPage({
        activeView,
        liveMode,
        logPageLoading: logState.loading,
        periodKey: document.querySelector("#log-period").value,
        rangeEndMs: periodBounds(document.querySelector("#log-period").value).endMs,
        nextRefreshAtMs: logState.nextRefreshAtMs,
        nowMs: Date.now()
      })
    ) {
      if (logState.page === 1 && document.querySelector("#log-period").value !== "custom") {
        logState.setSearchRange(null);
      }
      await loadLiveLogPage();
    }
    if (!document.querySelector("#channel-settings")?.contains(document.activeElement)) {
      renderChannelSettings();
    }
    renderOutputSettings(true);
    renderHealth();
    renderOutputTests();
    refreshCurrentStateCards();
    return next;
  }

  /**
   * 現在のserver stateを操作buttonやstatus表示へ同期する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function syncLiveControls() {
    const critical = fixtures.loggerStatus === "critical";
    const configurationBlocked = Boolean(fixtures.configurationSync?.outOfSync);
    const loggerStateKnown = ["logging", "maintenance", "critical"].includes(fixtures.loggerStatus);
    const badge = document.querySelector("#logging-badge");
    const toggle = document.querySelector("#toggle-logging");
    const state = document.querySelector("#maintenance-state");
    const banner = document.querySelector("#maintenance-banner");
    badge.classList.toggle("is-stopped", !logging);
    badge.classList.toggle("is-critical", critical);
    badge.querySelector("span:last-child").textContent = logging
      ? "記録中"
      : critical ? "Critical" : loggerStateKnown ? "記録停止中" : "状態不明";
    toggle.disabled = critical || !loggerStateKnown || (configurationBlocked && !logging);
    toggle.textContent = logging
      ? "記録を停止して出力テストへ"
      : critical ? "Critical状態：再起動が必要"
        : !loggerStateKnown ? "loggerd状態を確認できません"
          : configurationBlocked ? "設定確認まで再開できません" : "記録を再開";
    toggle.classList.toggle("danger-button", logging);
    toggle.classList.toggle(
      "primary-button",
      !logging && !critical && loggerStateKnown && !configurationBlocked
    );
    state.innerHTML = logging
      ? `<span class="status-dot status-dot--ok"></span><div><strong>記録中</strong><small>入力記録・動作確認信号・出力ルールが動作中</small></div>`
      : critical
        ? `<span class="status-dot status-dot--critical"></span><div><strong>異常停止（Critical）</strong><small>入力記録が停止しています。OUT状態は診断欄と実機で確認してください。loggerdを再起動するまで記録を再開できません。</small></div>`
        : !loggerStateKnown
          ? `<span class="status-dot status-dot--critical"></span><div><strong>loggerd状態不明</strong><small>通信できないため、入力記録とOUT状態を確認できません。実機のOUT状態を確認してください。</small></div>`
          : `<span class="status-dot status-dot--stopped"></span><div><strong>記録停止中（メンテナンス）</strong><small>${configurationBlocked && !fixtures.configurationSync?.stopped ? "入力記録とOUT状態を確認できません。実機のOUT状態を確認してください。" : "入力は記録されません。通常OUTはOFFです。設備の運転は停止していません。"}</small></div>`;
    banner.classList.toggle("banner--critical", critical);
    banner.classList.toggle("banner--warning", !loggerStateKnown);
    banner.querySelector(".banner-icon").textContent = critical ? "×" : "!";
    banner.querySelector("div strong").textContent = critical
      ? "異常停止（Critical）：loggerdの再起動が必要です"
      : !loggerStateKnown ? "loggerdと通信できません"
        : "メンテナンス中：入力は記録されていません";
    banner.hidden = logging && loggerStateKnown;
  }

  /**
   * 設定同期や保存状態の警告を表示する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderUnsynchronizedWarnings() {
    const count = fixtures.unsynchronizedCount;
    const latencyWarning = Boolean(fixtures.writeLatency?.warning);
    const latencyAttention = fixtures.writeLatency?.severity === "attention";
    const configurationWarning = Boolean(fixtures.configurationSync?.outOfSync);
    const storageWarning = storageWarningFor(fixtures.storage, fixtures.storageError);
    const diagnostics = activeView === "diagnostics";
    const notices = document.querySelector("#system-notices");
    const detail = document.querySelector("#unsynchronized-warning");
    const latencyDetail = document.querySelector("#write-latency-warning");
    const configurationDetail = document.querySelector("#configuration-sync-warning");
    const storageDetail = document.querySelector("#storage-warning-detail");
    const retryConfiguration = document.querySelector("#retry-config-sync");
    // 同じ注意をバナーと本文に二重表示せず、全画面共通のタイトル上へ集約する。
    notices.hidden = !(count > 0 || latencyWarning || configurationWarning
      || storageWarning.visible || (diagnostics && latencyAttention));
    document.querySelector("#notice-details-link").hidden = notices.hidden || diagnostics;
    [detail, latencyDetail, configurationDetail, storageDetail].forEach((notice) => {
      notice.querySelector("span").hidden = !diagnostics;
    });
    detail.hidden = count === 0;
    latencyDetail.hidden = !latencyWarning && !(diagnostics && latencyAttention);
    latencyDetail.querySelector("strong").textContent = latencyWarning
      ? "警告：ログ保存の状態を確認してください" : "注意：一時的なログ保存の遅延";
    latencyDetail.classList.toggle("diagnostic-warning--attention", latencyAttention);
    if (latencyWarning || latencyAttention) {
      latencyDetail.querySelector("span").textContent = `直近5分に500ms以上の保存が${fixtures.writeLatency.delayed_count_5m}回（最大${formatMilliseconds(fixtures.writeLatency.max_delay_ms_5m)}ms）、未保存の最長滞留${formatMilliseconds(fixtures.writeLatency.oldest_pending_ms)}ms。${latencyWarning ? "未保存の記録が失われるおそれがあります。入力頻度とストレージの状態を確認してください。" : "保存遅延のみでは記録の欠損を意味しません。未保存の記録は電源断で失われる可能性があります。"}`;
    }
    configurationDetail.hidden = !configurationWarning;
    storageDetail.hidden = !storageWarning.visible;
    storageDetail.classList.toggle("diagnostic-warning--critical", storageWarning.severity === "critical");
    storageDetail.querySelector("strong").textContent = storageWarning.title;
    storageDetail.querySelector("span").textContent = storageWarning.visible
      ? storageWarningMessage(storageWarning)
      : "";
    retryConfiguration.hidden = !diagnostics || !configurationWarning || !liveMode;
    if (count > 0) {
      const clockTitle = `同期前の記録が${count}件あります`;
      detail.querySelector("strong").textContent = clockTitle;
      detail.querySelector("span").textContent = `現在の時刻は同期済みのため、時刻設定は不要です。同期前の${count}件だけ、発生時刻が正確でない可能性があります。必要な記録の場合は、ログ画面で時刻が「要確認」の行を確認してください。`;
    }
    if (configurationWarning) {
      configurationDetail.querySelector("span").textContent = fixtures.configurationSync.message;
    }
  }

  /**
   * 現在設定中のtimezoneを返す。
   * @returns {string} 本体設定に保存された有効timezone名。未設定時は既定timezone。
   */
  function selectedTimeZone() {
    return fixtures.settings.timezone || "Asia/Tokyo";
  }

  const {
    timeZoneOffset,
    parseLocalDateTime,
    dateTimeParts,
    formatLocalDateTime,
    formatDateTimeInput
  } = window.EventLoggerTimeFormat.createTimeFormatter(selectedTimeZone);

  fixtures.events.forEach((event, index) => {
    event.id ||= fixtures.events.length - index;
  });

  /**
   * 信号IDに対応する設定名を返す。
   * @param {string} signalId - 信号ID。
   * @returns {string} IN/OUT signal IDに対応する設定名。未登録時はsignal IDそのもの。
   */
  function signalDisplayName(signalId) {
    const match = /^(IN|OUT)(\d+)$/.exec(signalId);
    if (!match) return signalId;
    const collection = match[1] === "IN" ? fixtures.channels : fixtures.outputs;
    const item = collection.find((entry) => entry.id === Number(match[2]));
    return item?.name || signalId;
  }

  const { signalIds, channelLabel, escapeHtml } = window.EventLoggerViewFormat;

  /**
   * switch elementの論理ON/OFF状態を読む。
   * @param {HTMLElement} button - 対象button element。
   * @returns {boolean} buttonのaria-checkedがtrueならtrue。
   */
  function switchIsOn(button) {
    return button.classList.contains("is-on");
  }

  /**
   * switchのaria状態と表示を指定状態へ更新する。
   * @param {HTMLElement} button - 対象button element。
   * @param {boolean} enabled - widgetを有効にするか。
   * @returns {boolean} buttonがON classを持ち、論理ONならtrue。
   */
  function setSwitchState(button, enabled) {
    button.classList.toggle("is-on", enabled);
    button.setAttribute("aria-checked", String(enabled));
  }

  /**
   * boolean設定用controlの現在値を読む。
   * @param {HTMLElement} button - 対象button element。
   * @returns {boolean} checkbox/switch controlの論理boolean値。
   */
  function boolControlValue(button) {
    return switchIsOn(button);
  }

  /**
   * widget編集switchの状態ラベルを初期化する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function initializeWidgetSwitchLabels() {
    document.querySelectorAll("#widget-settings-list .switch").forEach((button) => {
      button.dataset.onLabel ||= "表示中";
      button.dataset.offLabel ||= "非表示";
    });
  }

  /**
   * 短い利用者向け通知を表示し、時間後に閉じる。
   * @param {string} message - 利用者向け説明。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function showToast(message) {
    const toast = document.querySelector("#toast");
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2400);
  }

  /**
   * 指定viewを表示し、view依存データを更新する。
   * @param {string} viewName - 切替先の画面名。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function switchView(viewName) {
    activeView = viewName;
    renderUnsynchronizedWarnings();
    document.querySelectorAll(".view").forEach((view) => {
      view.classList.toggle("is-active", view.id === `view-${viewName}`);
    });
    document.querySelectorAll("[data-view]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.view === viewName);
    });
    if (viewName === "logs") {
      if (liveMode) loadLiveLogPage().catch((error) => showToast(error.message));
      else renderLogs();
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  /**
   * 選択timezoneに合わせて画面時計を描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderClock() {
    const nowMs = demoNowMs();
    const demoNow = new Date(nowMs);
    const clockText = new Intl.DateTimeFormat("ja-JP", {
      timeZone: selectedTimeZone(),
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }).format(demoNow);
    document.querySelector("#system-clock").textContent = clockText;
    const diagnosticClock = document.querySelector("#diagnostic-clock");
    if (diagnosticClock) diagnosticClock.textContent = formatLocalDateTime(nowMs).slice(0, -6);
    refreshCurrentStateCards();
    document.querySelectorAll('[data-custom-history-widget][data-widget-type="経過時間"], [data-custom-history-widget][data-widget-type="最終イベント"], [data-custom-history-widget][data-widget-type="ON/OFF時間"]').forEach(renderHistoryWidgetCard);
    renderOnTimeCard();
    renderLastEventCard();
  }

  /**
   * 直近eventをDashboard領域へ描画する。
   * @returns {void} 直近eventの一覧DOMを置換する。
   */
  function renderRecentEvents() {
    const card = document.querySelector('[data-widget="recent-events"]');
    const list = document.querySelector("#recent-events");
    if (!card || !list) return;
    const selectedSignals = signalIds(card.dataset.widgetChannel);
    const periodKey = periodKeyForWidget(card.dataset.widgetPeriod || "follow");
    const { startMs, endMs } = periodBounds(periodKey);
    const valueFilter = card.dataset.widgetValue || "all";
    const count = Number(card.dataset.widgetCount || 10);
    const events = fixtures.events.filter((event) => (
      ["edge", "output"].includes(event.kind)
      && selectedSignals.includes(event.signal)
      && (valueFilter === "all" || event.value === valueFilter)
      && eventTimeMs(event) >= startMs
      && eventTimeMs(event) <= endMs
    )).slice(0, count);
    list.innerHTML = events.map((event) => `
      <div class="event-item">
        <time class="event-time mono">${escapeHtml(formatLocalDateTime(eventTimeMs(event)).slice(11, 16))}</time>
        <strong class="event-name">${escapeHtml(signalDisplayName(event.signal))}</strong>
        <span class="value-pill value-pill--${escapeHtml(event.value.toLowerCase())}">${escapeHtml(event.value)}</span>
      </div>
    `).join("") || '<p class="custom-widget-empty">対象期間の記録はありません。</p>';
  }

  /**
   * 現在のtimeline表示期間の開始・終了を返す。
   * @returns {Object} startMs/endMsを持つ現在選択期間の半開範囲（millisecond）。
   */
  function timelinePeriodBounds() {
    const card = document.querySelector('[data-widget="timeline"]');
    return periodBounds(periodKeyForWidget(card.dataset.widgetPeriod || "follow"));
  }

  /**
   * timeline軸用の日時labelへ変換する。
   * @param {number} timestampMs - UTC Unix epoch millisecond。
   * @returns {string} 入力を画面またはAPIで使う形式へ変換した文字列。
   */
  function formatTimelineTime(timestampMs) {
    return formatLocalDateTime(timestampMs, true);
  }

  // 表示だけを丸め、記録時刻や集計値の精度は変更しない。
  /**
   * millisecond値を表示用文字列へ変換する。
   * @param {string} value - 変換対象の値。
   * @returns {string} 入力を画面またはAPIで使う形式へ変換した文字列。
   */
  function formatMilliseconds(value) {
    return Number(value).toFixed(1);
  }

  /**
   * 経過時間を読みやすい表示へ変換する。
   * @param {number} durationMs - 期間長（millisecond）。
   * @returns {string} 経過millisecondを人向け時間表示へ整形した文字列。
   */
  function formatDuration(durationMs) {
    if (durationMs < 1000) return `${formatMilliseconds(durationMs)}ミリ秒`;
    const totalSeconds = Math.round(durationMs / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    if (hours > 0) return `${hours}時間 ${minutes}分 ${seconds}秒`;
    if (minutes > 0) return `${minutes}分 ${seconds}秒`;
    return `${seconds}秒`;
  }

  /**
   * 経過時間を指定単位の文字列へ変換する。
   * @param {number} durationMs - 期間長（millisecond）。
   * @param {string} unit - 期間表示に使う単位。
   * @returns {string} 経過millisecondを指定unitで丸めた数値付き文字列。
   */
  function formatDurationForUnit(durationMs, unit = "human") {
    if (unit === "minutes") return `${Math.round(durationMs / 60000)}分`;
    if (unit === "hours_decimal") return `${(durationMs / 3600000).toFixed(2)}時間`;
    if (unit === "hms") {
      const totalSeconds = Math.max(0, Math.round(durationMs / 1000));
      const hours = Math.floor(totalSeconds / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);
      const seconds = totalSeconds % 60;
      return [hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");
    }
    return formatDuration(durationMs);
  }

  /**
   * 現在状態cardに表示する論理状態と文言を決める。
   * @param {string} channelId - 製品上のIN/OUT番号。
   * @param {boolean} onLabel - 論理ONに対応する表示文言。
   * @param {string} offLabel - 論理OFFに対応する表示文言。
   * @param {string} widgetId - widget ID。
   * @returns {Object} state、label、elapsed value、UNKNOWN表示をまとめた現在状態model。
   */
  function currentStateValues(channelId, onLabel, offLabel, widgetId) {
    const channel = fixtures.channels.find((item) => `IN${item.id}` === channelId);
    if (liveMode) {
      const dashboardValue = fixtures.dashboardValues?.find((item) => item.widget_id === widgetId);
      // 状態と継続時間は同じAPI snapshotを使い、画面起動時刻や直近ログを起点にしない。
      const value = logging && channel?.enabled && channel.value !== "UNKNOWN"
        ? dashboardValue?.state || "UNKNOWN"
        : "UNKNOWN";
      // API取得の合間だけ単調時計で表示を進め、次のsnapshotで基準を置き換える。
      const elapsedMs = value !== "UNKNOWN" && Number.isInteger(dashboardValue?.elapsed_ms)
        ? dashboardValue.elapsed_ms + Math.max(0, performance.now() - fixtures.dashboardReceivedAtMs)
        : null;
      return {
        value,
        displayValue: value === "ON" ? onLabel : value === "OFF" ? offLabel : "UNKNOWN",
        elapsed: elapsedMs === null ? "継続時間は計測していません" : `${formatDuration(elapsedMs)}継続`
      };
    }
    const value = logging && channel?.enabled ? channel.value : "UNKNOWN";
    const displayValue = value === "ON" ? onLabel : value === "OFF" ? offLabel : "UNKNOWN";
    const latestEvent = fixtures.events.find((event) => event.signal === channelId);
    const eventMs = Math.max(latestEvent ? eventTimeMs(latestEvent) : demoClockBaseMs, loggingStartedAtMs);
    return {
      value,
      displayValue,
      elapsed: value === "UNKNOWN" ? "継続時間は計測していません" : `${formatDuration(Math.max(0, demoNowMs() - eventMs))}継続`
    };
  }

  /**
   * 現在状態cardの値と表示を更新する。
   * @param {HTMLElement} card - 対象card element。
   * @param {Object} config - widget config。
   * @returns {void} cardのstate、ON/OFF表示、elapsed表示を更新する。
   */
  function updateCurrentStateCard(card, config) {
    const values = currentStateValues(config.channelId, config.onLabel, config.offLabel, config.id);
    card.dataset.widgetType = "現在状態";
    card.dataset.widgetChannel = config.channelId;
    card.dataset.onLabel = config.onLabel;
    card.dataset.offLabel = config.offLabel;
    card.dataset.showElapsed = String(config.showElapsed);
    card.classList.toggle("is-active-state", values.value === "ON");
    card.classList.toggle("is-normal-state", values.value === "OFF");
    card.querySelector("[data-widget-title]").textContent = config.title;
    card.querySelector("strong").textContent = values.displayValue;
    const elapsed = card.querySelector("p");
    elapsed.textContent = values.elapsed;
    elapsed.hidden = !config.showElapsed;
    card.querySelector("small").textContent = config.channelId;
  }

  /**
   * 現在状態widgetのHTMLを組み立てる。
   * @param {Object} config - widget config。
   * @returns {string} 表示labelと現在値を含むcurrent-state card HTML。
   */
  function currentStateCardHtml(config) {
    const values = currentStateValues(config.channelId, config.onLabel, config.offLabel, config.id);
    const hiddenClass = config.enabled === false ? " is-hidden-widget" : "";
    return `
      <article class="overview-card${hiddenClass}${values.value === "ON" ? " is-active-state" : values.value === "OFF" ? " is-normal-state" : ""}" data-widget="${escapeHtml(config.id)}" data-size="${escapeHtml(config.size)}" data-widget-type="現在状態" data-widget-channel="${escapeHtml(config.channelId)}" data-on-label="${escapeHtml(config.onLabel)}" data-off-label="${escapeHtml(config.offLabel)}" data-show-elapsed="${config.showElapsed}">
        <div class="overview-label" data-widget-title>${escapeHtml(config.title)}</div>
        <strong>${escapeHtml(values.displayValue)}</strong>
        <p${config.showElapsed ? "" : " hidden"}>${escapeHtml(values.elapsed)}</p>
        <small>${escapeHtml(config.channelId)}</small>
      </article>
    `;
  }

  /**
   * 現在状態widgetの設定rowを組み立てる。
   * @param {Object} config - widget config。
   * @returns {string} 設定一覧へ追加するcurrent-state row HTML。
   */
  function currentStateSettingRowHtml(config) {
    const enabled = config.enabled !== false;
    return `
      <div class="widget-setting-row is-highlighted" draggable="true" data-widget-row="${escapeHtml(config.id)}" data-api-config="${escapeHtml(JSON.stringify(config.apiConfig || {}))}" data-dashboard-size="${escapeHtml(config.size)}" data-widget-type="現在状態" data-widget-channel="${escapeHtml(config.channelId)}" data-on-label="${escapeHtml(config.onLabel)}" data-off-label="${escapeHtml(config.offLabel)}" data-show-elapsed="${config.showElapsed}">
        <span class="drag-handle" aria-hidden="true">⋮⋮</span>
        <div><strong>${escapeHtml(config.title)}</strong><small>現在状態・${escapeHtml(config.channelId)}${config.showElapsed ? "・継続時間を表示" : ""}</small></div>
        <label class="widget-size">表示幅<select aria-label="${escapeHtml(config.title)}の表示幅">${sizeOptionsHtml(config.size)}</select></label>
        <div class="row-move-buttons"><button class="icon-button move-up" type="button" aria-label="${escapeHtml(config.title)}を上へ移動">↑</button><button class="icon-button move-down" type="button" aria-label="${escapeHtml(config.title)}を下へ移動">↓</button></div>
        <div class="widget-row-actions"><button class="secondary-button small-button edit-widget" type="button">編集</button><button class="secondary-button small-button duplicate-widget" type="button">複製</button><button class="danger-text-button delete-widget" type="button">削除</button></div>
        <button class="switch${enabled ? " is-on" : ""}" type="button" role="switch" aria-checked="${enabled}" aria-label="${escapeHtml(config.title)}を表示" data-on-label="表示中" data-off-label="非表示"><span></span></button>
      </div>
    `;
  }

  /**
   * widget sizeの選択optionを組み立てる。
   * @param {string} selectedSize - 選択済みサイズ。
   * @returns {string} サイズ選択option一覧のHTML。
   */
  function sizeOptionsHtml(selectedSize) {
    const labels = { small: "小", medium: "中", large: "大" };
    return Object.entries(labels).map(([value, label]) => (
      `<option value="${value}"${value === selectedSize ? " selected" : ""}>${label}</option>`
    )).join("");
  }

  /**
   * 現在状態widget cardを最新fixture値で更新する。
   * @returns {void} DOM上の有効なcurrent-state cardを順に更新する。
   */
  function refreshCurrentStateCards() {
    document.querySelectorAll("#dashboard-realtime-grid [data-widget-type='現在状態']").forEach((card) => {
      updateCurrentStateCard(card, {
        id: card.dataset.widget,
        title: card.querySelector("[data-widget-title]").textContent,
        channelId: card.dataset.widgetChannel,
        onLabel: card.dataset.onLabel,
        offLabel: card.dataset.offLabel,
        showElapsed: card.dataset.showElapsed === "true"
      });
    });
  }

  /**
   * widget期間設定を計算用period keyへ変換する。
   * @param {string} periodSetting - 保存期間設定。
   * @returns {string} widget設定をfollow/customまたは許可済み固定period keyへ正規化した値。
   */
  function periodKeyForWidget(periodSetting) {
    return periodSetting === "follow" ? currentPeriod : periodSetting;
  }

  /**
   * period keyから表示対象時間境界を計算する。
   * @param {string} periodKey - 期間key。
   * @param {number} durationOverrideMs - 期間上書きmillisecond。
   * @returns {Object} startMs/endMsを持つ半開期間範囲。
   */
  function periodBounds(periodKey, durationOverrideMs = 0) {
    const period = fixtures.dashboardPeriods[periodKey];
    const endMs = period.endAt ? Date.parse(period.endAt) : demoNowMs();
    const durationMs = Number.isFinite(durationOverrideMs) && durationOverrideMs > 0
      ? durationOverrideMs
      : period.durationMs;
    const startMs = period.startAt ? Date.parse(period.startAt) : endMs - durationMs;
    return { startMs, endMs };
  }

  /**
   * event timestampをUnix epoch millisecondへ変換する。
   * @param {Object} event - fixture event。
   * @returns {number} event時刻のUTC Unix epoch millisecond。
   */
  function eventTimeMs(event) {
    return event.timestamp ? Date.parse(event.timestamp) : Date.parse(`2026-09-12T${event.time}+09:00`);
  }

  /**
   * 日時inputの値を検証し、カスタム期間を適用する。
   * @param {string} fromInput - 開始日時input。
   * @param {string} toInput - 終了日時input。
   * @returns {boolean} from/toを検証して期間適用できた時true。不正な入力ではfalse。
   */
  function applyCustomPeriod(fromInput, toInput) {
    const startMs = parseLocalDateTime(fromInput.value);
    const endMs = parseLocalDateTime(toInput.value);
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs >= endMs) {
      showToast("開始日時より後の終了日時を指定してください");
      return false;
    }
    fixtures.dashboardPeriods.custom.startAt = new Date(startMs).toISOString();
    fixtures.dashboardPeriods.custom.endAt = new Date(endMs).toISOString();
    return true;
  }

  /**
   * 信号と期間に一致するeventを抽出する。
   * @param {string} channelId - 製品上のIN/OUT番号。
   * @param {string} periodKey - 期間key。
   * @returns {Array<Object>} 指定signalと半開期間に入るevent fixture列。
   */
  function channelEventsInPeriod(channelId, periodKey) {
    const { startMs, endMs } = periodBounds(periodKey);
    return fixtures.events.filter((event) => {
      const timeMs = eventTimeMs(event);
      return event.signal === channelId && event.kind === "edge" && timeMs >= startMs && timeMs <= endMs;
    });
  }

  /**
   * 期間内で対象状態が継続した時間を合計する。
   * @param {string} channelId - 製品上のIN/OUT番号。
   * @param {string} periodKey - 期間key。
   * @param {string} targetState - 対象論理状態。
   * @param {number} durationOverrideMs - 期間上書きmillisecond。
   * @returns {number} 対象論理状態が占める期間長（millisecond）。
   */
  function stateDurationInPeriod(channelId, periodKey, targetState = "ON", durationOverrideMs = 0) {
    return timelineIntervals(channelId, periodKey, durationOverrideMs)
      .filter((interval) => interval.state === targetState)
      .reduce((total, interval) => total + interval.endMs - interval.startMs, 0);
  }

  /**
   * event列から状態・UNKNOWN区間を構成する。
   * @param {string} channelId - 製品上のIN/OUT番号。
   * @param {string} periodKey - 期間key。
   * @param {number} durationOverrideMs - 期間上書きmillisecond。
   * @returns {Array<Object>} 状態区間とUNKNOWN区間を時刻順に並べたtimeline配列。
   */
  function timelineIntervals(channelId, periodKey, durationOverrideMs = 0) {
    const bounds = periodBounds(periodKey, durationOverrideMs);
    const observedEndMs = Math.max(bounds.startMs, Math.min(bounds.endMs, loggingStoppedAtMs || demoNowMs()));
    if (bounds.endMs <= bounds.startMs) return [];
    const isOutput = channelId.startsWith("OUT");
    const events = fixtures.events
      .filter((event) => event.signal === channelId && (isOutput ? event.kind === "output" : ["edge", "snapshot"].includes(event.kind)) && eventTimeMs(event) <= observedEndMs)
      .sort((left, right) => eventTimeMs(left) - eventTimeMs(right));
    let state = "UNKNOWN";
    let source = "";
    let cursorMs = bounds.startMs;
    const intervals = [];
    events.forEach((event) => {
      const timeMs = eventTimeMs(event);
      if (timeMs <= bounds.startMs) {
        state = event.value;
        source = event.source || "";
        return;
      }
      intervals.push({ state, source, startMs: cursorMs, endMs: timeMs });
      state = event.value;
      source = event.source || "";
      cursorMs = timeMs;
    });
    intervals.push({ state, source, startMs: cursorMs, endMs: observedEndMs });
    if (observedEndMs < bounds.endMs) intervals.push({ state: "UNKNOWN", source: "", startMs: observedEndMs, endMs: bounds.endMs });
    let result = intervals.filter((interval) => interval.endMs > interval.startMs);
    unknownIntervals.forEach((gap) => {
      const gapStart = Math.max(bounds.startMs, gap.startMs);
      const gapEnd = Math.min(bounds.endMs, gap.endMs || demoNowMs());
      if (gapEnd <= gapStart) return;
      const next = [];
      result.forEach((interval) => {
        if (interval.endMs <= gapStart || interval.startMs >= gapEnd) {
          next.push(interval);
          return;
        }
        if (interval.startMs < gapStart) next.push({ ...interval, endMs: gapStart });
        next.push({ state: "UNKNOWN", source: "", startMs: Math.max(interval.startMs, gapStart), endMs: Math.min(interval.endMs, gapEnd) });
        if (interval.endMs > gapEnd) next.push({ ...interval, startMs: gapEnd });
      });
      result = next;
    });
    return result;
  }

  /**
   * 状態と幅に対応するtimeline CSS classを返す。
   * @param {string} classPrefix - CSS prefix。
   * @param {boolean} unknown - 区間がUNKNOWNかを表す真偽値。
   * @param {number} widthPercent - 全幅に対するpercentage。
   * @returns {string} 状態・UNKNOWN・幅に対応するCSS class文字列。
   */
  function timelineMarkClass(classPrefix, unknown, widthPercent) {
    // 長い期間では短い区間の幅が0px相当になり、hover/clickできなくなる。
    // 1%未満は時間幅を誇張せず、詳細を開ける固定マーカーで示す。
    const isNarrow = widthPercent < 1;
    if (unknown) {
      return `${classPrefix}-${isNarrow ? "unknown-marker" : "unknown"}`;
    }
    return `${classPrefix}-${isNarrow ? "marker" : "segment"}`;
  }

  /**
   * timeline intervalを表示するHTMLへ変換する。
   * @param {string} channelId - 製品上のIN/OUT番号。
   * @param {string} periodKey - 期間key。
   * @param {string} classPrefix - CSS prefix。
   * @param {number} durationOverrideMs - 期間上書きmillisecond。
   * @param {string} domainIntervals - API計算済みintervals。
   * @returns {string} 各timeline区間を示すHTML片。
   */
  function timelineMarksHtml(
    channelId,
    periodKey,
    classPrefix = "timeline",
    durationOverrideMs = 0,
    domainIntervals = null
  ) {
    const bounds = periodBounds(periodKey, durationOverrideMs);
    const durationMs = bounds.endMs - bounds.startMs;
    const name = signalDisplayName(channelId);
    const intervals = Array.isArray(domainIntervals)
      ? domainIntervals.map((interval) => ({
        state: interval.state,
        source: interval.source || "",
        startMs: Number(interval.start_ms),
        endMs: Number(interval.end_ms)
      }))
      : timelineIntervals(channelId, periodKey, durationOverrideMs);
    return intervals.filter((interval) => interval.state !== "OFF").map((interval) => {
      const start = Math.max(0, (interval.startMs - bounds.startMs) / durationMs * 100);
      const end = Math.min(100, (interval.endMs - bounds.startMs) / durationMs * 100);
      const unknown = interval.state === "UNKNOWN";
      const stateLabel = unknown ? "UNKNOWN区間" : "ON状態";
      const widthPercent = Math.max(0, end - start);
      const markClass = timelineMarkClass(classPrefix, unknown, widthPercent);
      const markerStyle = widthPercent < 1
        ? `left:${start}%;transform:translateX(-50%);`
        : `left:${start}%;width:${widthPercent}%;`;
      return `<button class="${markClass}" data-timeline-event data-kind="segment" data-state="${interval.state}" data-signal-id="${escapeHtml(channelId)}" data-name="${escapeHtml(name)}" data-source="${escapeHtml(interval.source)}" data-period="${escapeHtml(periodKey)}" data-start="${start}" data-end="${end}" data-start-ms="${interval.startMs}" data-end-ms="${interval.endMs}" type="button" aria-describedby="timeline-event-detail" aria-expanded="false" aria-label="${escapeHtml(name)} ${stateLabel}の詳細" style="${markerStyle}"></button>`;
    }).join("");
  }

  /**
   * 小型timelineのHTMLを構成する。
   * @param {string} channelId - 製品上のIN/OUT番号。
   * @param {string} periodKey - 期間key。
   * @param {number} durationOverrideMs - 期間上書きmillisecond。
   * @param {Object|null} dashboardValue - APIが返したtimeline interval集計。nullならfixtureからintervalを作る。
   * @returns {string} 小型timelineのlabelとintervalを含むHTML片。
   */
  function miniTimelineHtml(channelId, periodKey, durationOverrideMs = 0, dashboardValue = null) {
    const domainIntervals = dashboardValue?.channels?.find((item) => item.signal === channelId)?.intervals;
    const marks = timelineMarksHtml(
      channelId,
      periodKey,
      "custom-mini",
      durationOverrideMs,
      domainIntervals
    );
    return `<div class="custom-mini-timeline"><div class="custom-mini-track" data-mini-timeline-track>${marks}</div></div>`;
  }

  /**
   * 履歴widgetの種類に応じた本文HTMLを構成する。
   * @param {Object} config - widget config。
   * @returns {string} 履歴widget種別ごとの本文HTML。
   */
  function historyWidgetBodyHtml(config) {
    if (liveMode && fixtures.dashboardWarning) {
      return `<p class="custom-widget-empty" role="status">${escapeHtml(fixtures.dashboardWarning.message)}</p>`;
    }
    const periodKey = periodKeyForWidget(config.period);
    const periodLabel = fixtures.dashboardPeriods[periodKey].label;
    const bounds = periodBounds(periodKey, config.durationMs || 0);
    const selectedSignals = signalIds(config.channelId);
    const dashboardValue = liveMode
      ? fixtures.dashboardValues?.find((item) => item.widget_id === config.id)
      : null;
    const periodEvents = fixtures.events.filter((event) => (
      selectedSignals.includes(event.signal)
      && event.kind === "edge"
      && eventTimeMs(event) >= bounds.startMs
      && eventTimeMs(event) <= bounds.endMs
    ));
    if (config.type === "イベント回数") {
      const matchingEvents = fixtures.events.filter((event) => (
        event.signal === config.channelId
        && event.kind === "edge"
        && event.value === (config.edge || "ON")
      ));
      const mode = config.countMode || "period";
      const resetHistory = fixtures.counterResets[config.id] || [];
      const lastReset = resetHistory.at(-1);
      const count = liveMode && Number.isInteger(dashboardValue?.value)
        ? dashboardValue.value
        : mode === "stored_total"
        ? matchingEvents.length
        : mode === "since_reset"
          ? lastReset
            ? (lastReset.carriedCount || 0) + matchingEvents.filter((event) => event.id > (lastReset.afterEventId || 0)).length
            : 0
          : periodEvents.filter((event) => event.value === (config.edge || "ON")).length;
      const modeLabel = mode === "stored_total" ? "保存期間内累計" : mode === "since_reset" ? "リセット後" : periodLabel;
      const resetButton = mode === "since_reset" ? `<button class="secondary-button small-button" data-reset-counter="${escapeHtml(config.id)}" type="button">リセット</button>` : "";
      const resetLabel = liveMode && dashboardValue?.reset_at
        ? `<small>${escapeHtml(dashboardValue.reset_at)}にリセット</small>`
        : !liveMode && lastReset
          ? `<small>${escapeHtml(lastReset.label)}にリセット</small>`
          : "";
      return `<div class="custom-widget-value"><strong>${count}</strong><span>${escapeHtml(config.unit || "回")}</span></div><small>${escapeHtml(config.channelId)}・${escapeHtml(modeLabel)}</small>${resetLabel}${resetButton}`;
    }
    if (config.type === "ON/OFF時間") {
      const state = config.state || "ON";
      const duration = liveMode && Number.isInteger(dashboardValue?.duration_ms)
        ? dashboardValue.duration_ms
        : stateDurationInPeriod(config.channelId, periodKey, state, config.durationMs || 0);
      return `<div class="custom-widget-value"><strong>${escapeHtml(formatDurationForUnit(duration, config.timeUnit))}</strong></div><small>${escapeHtml(config.channelId)}・${escapeHtml(periodLabel)}・${state}</small>`;
    }
    if (config.type === "最終イベント") {
      const latest = liveMode
        ? Number.isInteger(dashboardValue?.timestamp_ms)
          ? dashboardValue.timestamp_ms
          : null
        : periodEvents.find((event) => event.value === (config.edge || "ON"));
      if (latest === null || latest === undefined) return `<div class="custom-widget-value"><strong>該当なし</strong></div><small>${escapeHtml(config.channelId)}・${escapeHtml(periodLabel)}</small>`;
      const latestMs = typeof latest === "number" ? latest : eventTimeMs(latest);
      const values = [];
      if (config.showAbsolute !== false) values.push(escapeHtml(formatLocalDateTime(latestMs).slice(11, 19)));
      const elapsedMs = liveMode && Number.isInteger(dashboardValue?.elapsed_ms)
        ? dashboardValue.elapsed_ms
        : Math.max(0, demoNowMs() - latestMs);
      if (config.showEventElapsed) values.push(escapeHtml(`${formatDuration(elapsedMs)}前`));
      return `<div class="custom-widget-value"><strong>${values.join(" / ") || "--"}</strong></div><small>${escapeHtml(config.channelId)}・${escapeHtml(periodLabel)}</small>`;
    }
    if (config.type === "経過時間") {
      let elapsedMs = null;
      if (liveMode && dashboardValue) {
        // UNKNOWN区間や実際のsession境界を扱うDomain計算を表示側で再実装しない。
        if (Number.isInteger(dashboardValue.elapsed_ms)) {
          elapsedMs = dashboardValue.elapsed_ms;
        }
      } else if (config.source === "event") {
        const latest = periodEvents.find((event) => event.value === (config.edge || "ON"));
        if (latest) elapsedMs = Math.max(0, demoNowMs() - eventTimeMs(latest));
      } else {
        const latestStateEvent = fixtures.events
          .filter((event) => event.signal === config.channelId && ["edge", "snapshot"].includes(event.kind))
          .sort((left, right) => eventTimeMs(left) - eventTimeMs(right))
          .at(-1);
        const currentChannel = fixtures.channels.find((channel) => `IN${channel.id}` === config.channelId);
        const targetState = config.state || "ON";
        if (currentChannel?.value === targetState && latestStateEvent?.value === targetState) {
          elapsedMs = Math.max(0, demoNowMs() - eventTimeMs(latestStateEvent));
        }
      }
      const elapsed = Number.isInteger(elapsedMs) ? formatDuration(elapsedMs) : "該当なし";
      return `<div class="custom-widget-value"><strong>${escapeHtml(elapsed)}</strong></div><small>${escapeHtml(config.channelId)}・${escapeHtml(periodLabel)}</small>`;
    }
    if (config.type === "タイムライン") {
      return `<div class="mini-timeline-toolbar"><span>${escapeHtml(periodLabel)}</span><div role="group" aria-label="タイムラインの拡大縮小"><button class="secondary-button small-button" data-mini-zoom="out" type="button">−</button><output data-mini-zoom-label>1倍</output><button class="secondary-button small-button" data-mini-zoom="in" type="button">＋</button></div></div><div class="custom-mini-layout"><div class="custom-mini-labels">${selectedSignals.map((channelId) => `<div class="custom-mini-label"><code>${escapeHtml(channelId)}</code></div>`).join("")}</div><div class="custom-mini-scroll" data-mini-timeline-scroll tabindex="0" role="region" aria-label="全信号共通のタイムライン横スクロール"><div data-mini-timeline-content>${selectedSignals.map((channelId) => miniTimelineHtml(channelId, periodKey, config.durationMs || 0, dashboardValue)).join("")}</div></div></div>`;
    }
    const valueFilter = config.value || "all";
    const eventListItems = liveMode && Array.isArray(dashboardValue?.events)
      ? dashboardValue.events.map((event) => ({
        ...event,
        timestamp: new Date(event.timestamp_ms).toISOString()
      }))
      : fixtures.events.filter((event) => (
        selectedSignals.includes(event.signal)
        && ["edge", "output"].includes(event.kind)
        && eventTimeMs(event) >= bounds.startMs
        && eventTimeMs(event) < bounds.endMs
      ));
    const items = eventListItems.filter((event) => valueFilter === "all" || event.value === valueFilter).slice(0, config.count || 10).map((event) => `
      <div class="event-item"><time class="event-time mono">${escapeHtml(formatLocalDateTime(eventTimeMs(event)).slice(11, 16))}</time><strong class="event-name">${escapeHtml(signalDisplayName(event.signal))}</strong><span class="value-pill value-pill--${event.value.toLowerCase()}">${event.value}</span></div>
    `).join("");
    return `<div class="event-list">${items || '<p class="custom-widget-empty">対象期間の記録はありません。</p>'}</div><small>${escapeHtml(config.channelId)}・${escapeHtml(periodLabel)}</small>`;
  }

  /**
   * 履歴widget cardの内容とevent handlerを描画する。
   * @param {HTMLElement} card - 対象card element。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderHistoryWidgetCard(card) {
    if (card.dataset.widgetType === "タイムライン") {
      const focusedEvent = document.activeElement?.matches("[data-timeline-event]")
        ? document.activeElement
        : null;
      const hoveredEvent = document.querySelector("[data-timeline-event]:hover");
      const detail = document.querySelector("#timeline-event-detail");
      if (shouldDeferTimelineWidgetRender({
        containsPinnedEvent: Boolean(pinnedTimelineEvent && card.contains(pinnedTimelineEvent)),
        containsFocusedEvent: Boolean(focusedEvent && card.contains(focusedEvent)),
        containsHoveredEvent: Boolean(hoveredEvent && card.contains(hoveredEvent)),
        containsDetailEvent: Boolean(
          detail
          && !detail.hidden
          && activeTimelineDetailEvent
          && card.contains(activeTimelineDetailEvent)
        )
      })) {
        // custom timelineも定期更新のたびにbodyを置換すると、固定した区間と
        // focus先が切り離される。操作が終われば次のrefreshで最新状態に戻す。
        return;
      }
    }
    // 定期更新でDOMを作り直しても、全信号の共通表示位置を維持する。
    const scrollLeft = card.querySelector("[data-mini-timeline-scroll]")?.scrollLeft || 0;
    card.querySelector("[data-history-widget-body]").innerHTML = historyWidgetBodyHtml({
      id: card.dataset.widget,
      type: card.dataset.widgetType,
      channelId: card.dataset.widgetChannel,
      period: card.dataset.widgetPeriod,
      edge: card.dataset.widgetEdge,
      state: card.dataset.widgetState,
      count: Number(card.dataset.widgetCount || 10),
      value: card.dataset.widgetValue || "all",
      countMode: card.dataset.widgetCountMode || "period",
      unit: card.dataset.widgetUnit || "回",
      timeUnit: card.dataset.widgetTimeUnit || "human",
      source: card.dataset.widgetSource || "event",
      durationMs: Number(card.dataset.widgetDurationMs || 0),
      showAbsolute: card.dataset.showAbsolute !== "false",
      showEventElapsed: card.dataset.showEventElapsed === "true"
    });
    applyMiniTimelineZoom(card);
    const scroll = card.querySelector("[data-mini-timeline-scroll]");
    if (scroll) scroll.scrollLeft = scrollLeft;
    bindTimelineEvents();
  }

  /**
   * 小型timelineの表示倍率を更新する。
   * @param {HTMLElement} card - 対象card element。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function applyMiniTimelineZoom(card) {
    const label = card.querySelector("[data-mini-zoom-label]");
    if (!label) return;
    const index = Math.max(0, Math.min(timelineZoomLevels.length - 1, Number(card.dataset.miniZoomIndex || 0)));
    card.dataset.miniZoomIndex = String(index);
    const zoom = timelineZoomLevels[index];
    // 行別の幅やスクロールを持たせず、全信号を同じ時間軸で拡大する。
    card.querySelector("[data-mini-timeline-content]").style.width = `${zoom * 100}%`;
    if (index === 0) card.querySelector("[data-mini-timeline-scroll]").scrollLeft = 0;
    label.textContent = `${zoom}倍`;
    card.querySelector('[data-mini-zoom="out"]').disabled = index === 0;
    card.querySelector('[data-mini-zoom="in"]').disabled = index === timelineZoomLevels.length - 1;
  }

  /**
   * 保存済みの独自履歴widget群を描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderCustomHistoryWidgets() {
    document.querySelectorAll("[data-custom-history-widget]").forEach(renderHistoryWidgetCard);
  }

  /**
   * 履歴widget cardのHTMLを組み立てる。
   * @param {Object} config - widget config。
   * @returns {string} 履歴widget cardのHTML。
   */
  function historyWidgetCardHtml(config) {
    const hiddenClass = config.enabled === false ? " is-hidden-widget" : "";
    const logLink = ["タイムライン", "イベント一覧"].includes(config.type)
      ? '<button class="link-button" data-custom-log-link type="button">ログで詳しく見る</button>'
      : "";
    return `
      <article class="panel custom-history-widget${hiddenClass}" data-custom-history-widget data-widget="${escapeHtml(config.id)}" data-size="${escapeHtml(config.size)}" data-widget-type="${escapeHtml(config.type)}" data-widget-channel="${escapeHtml(config.channelId)}" data-widget-period="${escapeHtml(config.period)}" data-widget-edge="${escapeHtml(config.edge || "ON")}" data-widget-state="${escapeHtml(config.state || "ON")}" data-widget-source="${escapeHtml(config.source || "event")}" data-widget-duration-ms="${escapeHtml(config.durationMs || "")}" data-widget-count="${escapeHtml(config.count || 10)}" data-widget-value="${escapeHtml(config.value || "all")}" data-widget-count-mode="${escapeHtml(config.countMode || "period")}" data-widget-unit="${escapeHtml(config.unit || "回")}" data-widget-time-unit="${escapeHtml(config.timeUnit || "human")}" data-show-absolute="${config.showAbsolute !== false}" data-show-event-elapsed="${Boolean(config.showEventElapsed)}">
        <div class="panel-head"><div><h2 data-widget-title>${escapeHtml(config.title)}</h2>${logLink}</div></div>
        <div data-history-widget-body>${historyWidgetBodyHtml(config)}</div>
      </article>
    `;
  }

  /**
   * 履歴widget設定rowのHTMLを組み立てる。
   * @param {Object} config - widget config。
   * @returns {string} 履歴widget設定rowのHTML。
   */
  function historyWidgetSettingRowHtml(config) {
    const summary = widgetSettingSummary(config.type, config.channelId, config.period, config);
    const enabled = config.enabled !== false;
    return `
      <div class="widget-setting-row is-highlighted" draggable="true" data-widget-row="${escapeHtml(config.id)}" data-dashboard-size="${escapeHtml(config.size)}" data-api-config="${escapeHtml(JSON.stringify(config.apiConfig || {}))}" data-widget-type="${escapeHtml(config.type)}" data-widget-channel="${escapeHtml(config.channelId)}" data-widget-period="${escapeHtml(config.period)}" data-widget-edge="${escapeHtml(config.edge || "ON")}" data-widget-state="${escapeHtml(config.state || "ON")}" data-widget-source="${escapeHtml(config.source || "event")}" data-widget-duration-ms="${escapeHtml(config.durationMs || "")}" data-widget-count="${escapeHtml(config.count || 10)}" data-widget-value="${escapeHtml(config.value || "all")}" data-widget-count-mode="${escapeHtml(config.countMode || "period")}" data-widget-unit="${escapeHtml(config.unit || "回")}" data-widget-time-unit="${escapeHtml(config.timeUnit || "human")}" data-show-absolute="${config.showAbsolute !== false}" data-show-event-elapsed="${Boolean(config.showEventElapsed)}">
        <span class="drag-handle" aria-hidden="true">⋮⋮</span>
        <div><strong>${escapeHtml(config.title)}</strong><small>${escapeHtml(summary)}</small></div>
        <label class="widget-size">表示幅<select aria-label="${escapeHtml(config.title)}の表示幅">${sizeOptionsHtml(config.size)}</select></label>
        <div class="row-move-buttons"><button class="icon-button move-up" type="button" aria-label="${escapeHtml(config.title)}を上へ移動">↑</button><button class="icon-button move-down" type="button" aria-label="${escapeHtml(config.title)}を下へ移動">↓</button></div>
        <div class="widget-row-actions"><button class="secondary-button small-button edit-widget" type="button">編集</button><button class="secondary-button small-button duplicate-widget" type="button">複製</button><button class="danger-text-button delete-widget" type="button">削除</button></div>
        <button class="switch${enabled ? " is-on" : ""}" type="button" role="switch" aria-checked="${enabled}" aria-label="${escapeHtml(config.title)}を表示" data-on-label="表示中" data-off-label="非表示"><span></span></button>
      </div>
    `;
  }

  // DOMに依存しない表示項目の相互変換は別スクリプトで管理する。
  const { widgetConfigFromApi, widgetPayloadFromConfig } =
    window.EventLoggerWidgetConfig;

  /**
   * 編集rowの値をwidget保存API payloadへ変換する。
   * @param {HTMLElement} row - widget row element。
   * @param {number} sortOrder - 保存表示順。
   * @returns {Object} widget保存API用payload。row値とsort orderを使う。
   */
  function widgetApiPayloadFromRow(row, sortOrder) {
    // DOMの読取りはUI側、形式変換は専用モジュールに分離する。
    return widgetPayloadFromConfig(
      widgetConfigFromRow(row),
      sortOrder,
      switchIsOn(row.querySelector(".switch")),
      row.dataset.widgetSource
    );
  }

  /**
   * 保存済みwidget設定を編集一覧へ反映する。
   * @param {Array<Object>} items - APIまたはfixtureから受け取ったwidget列。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderPersistedWidgets(items) {
    const realtimeList = document.querySelector('[data-widget-group="realtime"]');
    const historyList = document.querySelector('[data-widget-group="history"]');
    const realtimeDashboard = document.querySelector("#dashboard-realtime-grid");
    const historyDashboard = document.querySelector("#dashboard-history-grid");
    realtimeList.replaceChildren();
    historyList.replaceChildren();
    realtimeDashboard.replaceChildren();
    historyDashboard.replaceChildren();
    (items || []).forEach((item) => {
      const config = widgetConfigFromApi(item);
      if (config.type === "現在状態") {
        realtimeList.insertAdjacentHTML("beforeend", currentStateSettingRowHtml(config));
        realtimeDashboard.insertAdjacentHTML("beforeend", currentStateCardHtml(config));
      } else {
        historyList.insertAdjacentHTML("beforeend", historyWidgetSettingRowHtml(config));
        historyDashboard.insertAdjacentHTML("beforeend", historyWidgetCardHtml(config));
      }
    });
    initializeWidgetSwitchLabels();
    syncDashboardLayout();
    renderCustomHistoryWidgets();
  }

  /**
   * widget設定に未保存変更がある状態を記録する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function markWidgetSettingsDirty() {
    if (liveMode) widgetEditor.dirty = true;
  }

  /**
   * 編集内容をwidget APIへ保存し、結果を画面へ反映する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  async function saveWidgetSettings() {
    if (!liveMode || !widgetEditor.dirty) return;
    // 途中の通信失敗で一部だけ保存された場合も、再試行時はDBの最新状態を
    // 基準に差分を作る。古い一覧のままPOSTすると重複IDで復旧できなくなる。
    const current = await liveJson("/api/widgets");
    fixtures.widgets = current.items || [];
    // 行を直接全件POSTすると、更新済みIDと新規IDを区別できない。最新DB一覧を
    // 基準にupsertし、画面から消えたwidgetだけを最後に削除する。
    const currentById = new Map(fixtures.widgets.map((widget) => [widget.id, widget]));
    const rowsById = new Set();
    for (const group of document.querySelectorAll("#widget-settings-list .widget-group-list")) {
      const rows = [...group.querySelectorAll(".widget-setting-row")];
      for (const [sortOrder, row] of rows.entries()) {
        const payload = widgetApiPayloadFromRow(row, sortOrder);
        rowsById.add(payload.id);
        const existing = currentById.get(payload.id);
        const method = existing ? "PUT" : "POST";
        const path = existing
          ? `/api/widgets/${encodeURIComponent(payload.id)}`
          : "/api/widgets";
        await liveJson(path, { method, body: JSON.stringify(payload) });
      }
    }
    for (const widget of currentById.values()) {
      if (!rowsById.has(widget.id)) {
        await liveJson(`/api/widgets/${encodeURIComponent(widget.id)}`, { method: "DELETE" });
      }
    }
    // 部分成功した通信状態でも、保存結果を再読込してUIとDBを一致させてから
    // dirty flagを落とす。途中throwならdirtyを保持し、次回は再び最新値から始める。
    const response = await liveJson("/api/widgets");
    fixtures.widgets = response.items || [];
    widgetEditor.dirty = false;
    renderPersistedWidgets(fixtures.widgets);
  }

  // rolling期間の境界は更新されるため、描画済み区間の詳細は絶対時刻を優先する。
  /**
   * timeline event群の時刻範囲を算出する。
   * @param {DOMStringMap} dataset - DOM data属性。
   * @param {string} periodBoundsValue - 期間境界値。
   * @returns {Object|null} datasetから復元したevent期間のstart/end millisecond。必要な境界がなければnull。
   */
  function timelineEventTimeBounds(dataset, periodBoundsValue) {
    const periodDurationMs = periodBoundsValue.endMs - periodBoundsValue.startMs;
    const storedStartMs = Number(dataset.startMs);
    const eventStartMs = Number.isFinite(storedStartMs)
      ? storedStartMs
      : periodBoundsValue.startMs + periodDurationMs * Number(dataset.start) / 100;
    const storedEndMs = Number(dataset.endMs);
    const eventEndMs = Number.isFinite(storedEndMs)
      ? storedEndMs
      : dataset.kind === "pulse"
        ? eventStartMs + Number(dataset.durationMs)
        : periodBoundsValue.startMs + periodDurationMs * Number(dataset.end) / 100;
    return { eventStartMs, eventEndMs };
  }

  /**
   * buttonに対応するevent詳細を取得する。
   * @param {HTMLElement} button - 対象button element。
   * @returns {Object|null} button data attributeから組み立てたtimestamp、signal、value等の表示用details。
   */
  function timelineEventDetails(button) {
    const periodBoundsValue = button.dataset.period
      ? periodBounds(button.dataset.period)
      : timelinePeriodBounds();
    const { eventStartMs, eventEndMs } = timelineEventTimeBounds(
      button.dataset,
      periodBoundsValue
    );
    return {
      name: button.dataset.name,
      signalId: button.dataset.signalId,
      content: button.dataset.state === "UNKNOWN" ? "監視できていない区間" : button.dataset.kind === "pulse" ? "ONパルス" : "ON状態",
      start: formatTimelineTime(eventStartMs),
      end: formatTimelineTime(eventEndMs),
      duration: formatDuration(Math.max(0, Math.round(eventEndMs - eventStartMs))),
      source: button.dataset.source || ""
    };
  }

  /**
   * 詳細popupをviewport内で読みやすい位置へ配置する。
   * @param {HTMLElement} button - 対象button element。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function positionTimelineDetail(button) {
    const detail = document.querySelector("#timeline-event-detail");
    const rect = button.getBoundingClientRect();
    const margin = 10;
    const maxLeft = window.innerWidth - detail.offsetWidth - margin;
    const left = Math.max(margin, Math.min(rect.left + rect.width / 2 - detail.offsetWidth / 2, maxLeft));
    const above = rect.top - detail.offsetHeight - margin;
    const top = above >= margin ? above : rect.bottom + margin;
    detail.style.left = `${left}px`;
    detail.style.top = `${top}px`;
  }

  /**
   * timeline event詳細popupを表示し、必要なら固定する。
   * @param {HTMLElement} button - 対象button element。
   * @param {boolean} pin - 表示を固定するか。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function showTimelineDetail(button, pin = false) {
    const detail = document.querySelector("#timeline-event-detail");
    const timelineEvent = timelineEventDetails(button);
    activeTimelineDetailEvent = button;
    if (pin) {
      document.querySelectorAll("[data-timeline-event]").forEach((item) => {
        item.classList.toggle("is-selected", item === button);
        item.setAttribute("aria-expanded", String(item === button));
      });
      pinnedTimelineEvent = button;
    }
    detail.innerHTML = `
      <div class="timeline-detail-head"><strong>${escapeHtml(timelineEvent.name)}</strong><code>${escapeHtml(timelineEvent.signalId)}</code></div>
      <dl>
        <div><dt>内容</dt><dd>${escapeHtml(timelineEvent.content)}</dd></div>
        <div><dt>発生時間</dt><dd class="mono">${escapeHtml(timelineEvent.start)}</dd></div>
        <div><dt>終了時間</dt><dd class="mono">${escapeHtml(timelineEvent.end)}</dd></div>
        <div><dt>継続時間</dt><dd>${escapeHtml(timelineEvent.duration)}</dd></div>
        ${timelineEvent.source ? `<div><dt>source</dt><dd>${escapeHtml(timelineEvent.source)}</dd></div>` : ""}
      </dl>
      ${pin ? '<small>クリックで固定中・Escで閉じる</small>' : ''}
    `;
    detail.hidden = false;
    positionTimelineDetail(button);
  }

  /**
   * timeline event詳細popupを条件に応じて閉じる。
   * @param {boolean} force - 強制的に閉じるか。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function hideTimelineDetail(force = false) {
    if (pinnedTimelineEvent && !force) return;
    document.querySelector("#timeline-event-detail").hidden = true;
    activeTimelineDetailEvent = null;
    if (force) {
      document.querySelectorAll("[data-timeline-event]").forEach((item) => {
        item.classList.remove("is-selected");
        item.setAttribute("aria-expanded", "false");
      });
      pinnedTimelineEvent = null;
    }
  }

  /**
   * 未bindのtimeline eventへpointer、focus、click handlerを登録する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function bindTimelineEvents() {
    document.querySelectorAll("[data-timeline-event]:not([data-timeline-bound])").forEach((button) => {
      button.dataset.timelineBound = "true";
      button.addEventListener("mouseenter", () => {
        if (!pinnedTimelineEvent) showTimelineDetail(button);
      });
      button.addEventListener("mouseleave", () => hideTimelineDetail());
      button.addEventListener("focus", () => {
        focusedTimelineEvent = true;
        if (!pinnedTimelineEvent) showTimelineDetail(button);
      });
      button.addEventListener("blur", () => {
        focusedTimelineEvent = false;
        hideTimelineDetail();
      });
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        if (pinnedTimelineEvent === button) {
          hideTimelineDetail(true);
          return;
        }
        showTimelineDetail(button, true);
      });
    });
  }

  /**
   * 選択信号・期間のtimeline軸と各rowを描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderTimeline() {
    const card = document.querySelector('[data-widget="timeline"]');
    const timeline = document.querySelector("#timeline");
    if (!card || !timeline) return;
    if (liveMode && fixtures.dashboardWarning) {
      focusedTimelineEvent = false;
      timeline.innerHTML = `<p class="custom-widget-empty" role="status">${escapeHtml(fixtures.dashboardWarning.message)}</p>`;
      hideTimelineDetail(true);
      return;
    }
    const detail = document.querySelector("#timeline-event-detail");
    if (shouldDeferTimelineRender({
      pinned: Boolean(pinnedTimelineEvent),
      focused: focusedTimelineEvent || document.activeElement?.matches("[data-timeline-event]"),
      detailVisible: Boolean(detail && !detail.hidden)
    })) {
      // rolling periodの再描画がhover/focus中のbuttonを毎秒置換すると、
      // 詳細が消え、キーボード操作中のfocusも失われる。操作が終わるまで
      // 現在の区間DOMを保ち、mouseleave/blur/Esc後の次回更新で追従する。
      return;
    }
    // 操作中DOMを維持できると判断した後で期間・軸・signal rowを再構築する。
    // 先にinnerHTMLを置き換えるとpinned detailやkeyboard focusが失われる。
    const periodKey = periodKeyForWidget(card.dataset.widgetPeriod || "follow");
    const bounds = periodBounds(periodKey);
    const axis = Array.from({ length: 5 }, (_, index) => {
      const timestamp = bounds.startMs + (bounds.endMs - bounds.startMs) * index / 4;
      return new Intl.DateTimeFormat("ja-JP", { timeZone: selectedTimeZone(), month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(timestamp));
    });
    const selectedSignals = signalIds(card.dataset.widgetChannel);
    const rows = selectedSignals.map((signalId) => {
      const signalName = signalDisplayName(signalId);
      const marks = timelineMarksHtml(signalId, periodKey);
      return `
        <div class="timeline-row">
          <div class="timeline-label"><code>${escapeHtml(signalId)}</code><span>${escapeHtml(signalName)}</span></div>
          <div class="timeline-track">${marks}</div>
        </div>
      `;
    }).join("");
    timeline.innerHTML = `
      <div class="timeline-axis"><span></span>${axis.map((label) => `<span>${escapeHtml(label)}</span>`).join("")}</div>
      ${rows || '<p class="custom-widget-empty">表示する信号が選択されていません。</p>'}
      <div class="timeline-legend"><span>灰色：OFF</span><span>青：ON</span><span>斜線：UNKNOWN</span></div>
      <p class="timeline-help">色付きの箇所にマウスを合わせるか、クリックすると詳細を表示します。</p>
    `;
    hideTimelineDetail(true);
    bindTimelineEvents();
    applyTimelineZoom();
  }

  /**
   * timeline拡大率に応じて表示範囲を更新する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function applyTimelineZoom() {
    const timeline = document.querySelector("#timeline");
    const zoomLabel = document.querySelector("#timeline-zoom-label");
    const zoomOut = document.querySelector("#timeline-zoom-out");
    const zoomIn = document.querySelector("#timeline-zoom-in");
    const timelineScroll = document.querySelector("#timeline-scroll");
    if (!timeline || !zoomLabel || !zoomOut || !zoomIn || !timelineScroll) return;
    const zoom = timelineZoomLevels[timelineZoomIndex];
    timeline.style.width = `${zoom * 100}%`;
    zoomLabel.textContent = `${zoom}倍`;
    zoomOut.disabled = timelineZoomIndex === 0;
    zoomIn.disabled = timelineZoomIndex === timelineZoomLevels.length - 1;
    if (timelineZoomIndex === 0) timelineScroll.scrollLeft = 0;
  }

  /**
   * 選択期間に応じてdashboard集計widgetを再描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderDashboardPeriod() {
    const countCard = document.querySelector('[data-widget="cycle-count"]');
    if (countCard && document.querySelector("#cycle-count") && document.querySelector("#count-period-label")) {
      const countPeriodKey = periodKeyForWidget(countCard.dataset.widgetPeriod || "follow");
      const countEvents = channelEventsInPeriod(countCard.dataset.widgetChannel, countPeriodKey);
      const allMatchingEvents = fixtures.events.filter((event) => (
        event.signal === countCard.dataset.widgetChannel
        && event.kind === "edge"
        && event.value === (countCard.dataset.widgetEdge || "ON")
      ));
      const mode = countCard.dataset.widgetCountMode || "period";
      const resetHistory = fixtures.counterResets[countCard.dataset.widget] || [];
      const lastReset = resetHistory.at(-1);
      const count = mode === "stored_total"
        ? allMatchingEvents.length
        : mode === "since_reset"
          ? (lastReset?.carriedCount || 0) + allMatchingEvents.filter((event) => event.id > (lastReset?.afterEventId || 0)).length
          : countEvents.filter((event) => event.value === (countCard.dataset.widgetEdge || "ON")).length;
      document.querySelector("#cycle-count").textContent = String(count);
      countCard.querySelector(".summary-value span").textContent = countCard.dataset.widgetUnit || "回";
      document.querySelector("#count-period-label").textContent = mode === "stored_total" ? "保存期間内累計" : mode === "since_reset" ? "リセット後" : fixtures.dashboardPeriods[countPeriodKey].label;
      countCard.querySelector("[data-reset-counter]")?.remove();
      countCard.querySelector("[data-reset-label]")?.remove();
      if (lastReset) countCard.insertAdjacentHTML("beforeend", `<small data-reset-label>${escapeHtml(lastReset.label)}にリセット</small>`);
      if (mode === "since_reset") countCard.insertAdjacentHTML("beforeend", `<button class="secondary-button small-button" data-reset-counter="${escapeHtml(countCard.dataset.widget)}" type="button">リセット</button>`);
    }

    renderOnTimeCard();

    const timelineCard = document.querySelector('[data-widget="timeline"]');
    if (timelineCard && document.querySelector("#timeline")) {
      const timelinePeriodKey = periodKeyForWidget(timelineCard.dataset.widgetPeriod || "follow");
      document.querySelector("#timeline").setAttribute("aria-label", `${fixtures.dashboardPeriods[timelinePeriodKey].label}の信号タイムライン`);
      renderTimeline();
    }
    renderRecentEvents();
  }

  /**
   * ON時間集計cardを描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderOnTimeCard() {
    const timeCard = document.querySelector('[data-widget="on-time"]');
    if (timeCard && document.querySelector("#on-time-value") && document.querySelector("#on-time-period-label")) {
      const timePeriodKey = periodKeyForWidget(timeCard.dataset.widgetPeriod || "follow");
      const targetState = timeCard.dataset.widgetState || "ON";
      const unit = timeCard.dataset.widgetTimeUnit || "human";
      document.querySelector("#on-time-value").textContent = formatDurationForUnit(stateDurationInPeriod(timeCard.dataset.widgetChannel, timePeriodKey, targetState), unit);
      document.querySelector("#on-time-period-label").textContent = `${targetState}・${fixtures.dashboardPeriods[timePeriodKey].label}`;
    }
    renderLastEventCard();
  }

  /**
   * 最後の一致event cardを描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderLastEventCard() {
    const lastCard = document.querySelector('[data-widget="last-alarm"]');
    if (!lastCard || !document.querySelector("#last-alarm-value") || !document.querySelector("#last-alarm-period-label")) return;
    const lastPeriodKey = periodKeyForWidget(lastCard.dataset.widgetPeriod || "follow");
    const lastEdge = lastCard.dataset.widgetEdge || "ON";
    const latest = channelEventsInPeriod(lastCard.dataset.widgetChannel, lastPeriodKey).find((event) => event.value === lastEdge);
    const values = [];
    if (lastCard.dataset.showAbsolute !== "false" && latest) values.push(formatLocalDateTime(eventTimeMs(latest)).slice(11, 19));
    if (lastCard.dataset.showEventElapsed === "true" && latest) values.push(`${formatDuration(Math.max(0, demoNowMs() - eventTimeMs(latest)))}前`);
    document.querySelector("#last-alarm-value").textContent = values.join(" / ") || "該当なし";
    document.querySelector("#last-alarm-period-label").textContent = `${lastCard.dataset.widgetChannel}・${fixtures.dashboardPeriods[lastPeriodKey].label}`;
  }

  /**
   * 出力状態card一覧を描画する。
   * @returns {void} output fixtureの状態・ownerを各output cardへ反映する。
   */
  function renderDashboardOutputs() {
    const container = document.querySelector("#dashboard-outputs");
    if (!container) return;
    container.innerHTML = fixtures.outputs.map((output) => `
      <div class="output-card">
        <strong>OUT${output.id}</strong>
        <span class="value-pill value-pill--${escapeHtml(output.value.toLowerCase())}">${escapeHtml(output.value)}</span>
        <small>${escapeHtml(output.owner)}</small>
      </div>
    `).join("");
  }

  /**
   * event kindを画面用labelへ変換する。
   * @param {Object} event - fixture event。
   * @returns {string} event kindの画面表示label。
   */
  function kindLabel(event) {
    if (event.kind === "output") return "出力の記録";
    if (event.kind === "snapshot") return "記録開始時の状態";
    return "入力の変化";
  }

  /**
   * 現在の検索条件に一致するeventを返す。
   * @returns {Array<Object>} 現在のfilter条件に一致するevent fixture列。
   */
  function filteredEvents() {
    const periodKey = document.querySelector("#log-period").value;
    const { startMs, endMs } = periodBounds(periodKey);
    const signal = document.querySelector("#log-signal").value;
    const value = document.querySelector("#log-value").value;
    const kind = document.querySelector("#log-kind").value;
    const source = document.querySelector("#log-source").value;
    return fixtures.events.filter((event) => (
      eventTimeMs(event) >= startMs
      && eventTimeMs(event) <= endMs
      && (
        signal === "all"
        || (signal === "selection" && widgetLogSignals?.includes(event.signal))
        || event.signal === signal
      )
      && (value === "all" || event.value === value)
      && (kind === "all" || event.kind === kind)
      && (source === "all" || event.source === source)
    ));
  }

  /**
   * ログ検索で選択可能な入力・出力signalを描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderLogSignalOptions() {
    const select = document.querySelector("#log-signal");
    const selected = select.value || "all";
    const inputs = fixtures.channels.map((channel) => (
      `<option value="IN${channel.id}">${channel.name ? `${escapeHtml(channel.name)}（IN${channel.id}）` : `IN${channel.id}（名称未設定）`}</option>`
    ));
    const outputs = fixtures.outputs.map((output) => (
      `<option value="OUT${output.id}">${escapeHtml(output.name || `OUT${output.id}`)}（OUT${output.id}）</option>`
    ));
    const selection = widgetLogSignals?.length
      ? `<option value="selection">選択中の信号（${escapeHtml(widgetLogSignals.join("・"))}）</option>`
      : "";
    select.innerHTML = `<option value="all">すべての信号</option>${selection}${inputs.join("")}${outputs.join("")}`;
    select.value = [...select.options].some((option) => option.value === selected) ? selected : "all";
  }

  /**
   * 現在の条件のログ一覧とpaging表示を描画する。
   * @returns {void} 現在のquery結果、page操作、空結果案内をlogs viewへ描画する。
   */
  function renderLogs() {
    const pageSize = Number(document.querySelector("#log-page-size").value);
    const events = liveMode ? logState.events : filteredEvents();
    const totalCount = liveMode ? logState.totalCount : events.length;
    const pageCount = Math.max(1, Math.ceil(totalCount / pageSize));
    logState.setPage(Math.min(logState.page, pageCount));
    const pageEvents = liveMode
      ? events
      : events.slice((logState.page - 1) * pageSize, logState.page * pageSize);
    const tbody = document.querySelector("#log-table-body");
    document.querySelector("#log-count").textContent = liveMode && !logState.loaded
      ? "--件"
      : `${totalCount}件`;
    document.querySelector("#log-page-label").textContent = `${logState.page} / ${pageCount}ページ`;
    document.querySelector("#log-prev").disabled = logState.loading || logState.page === 1;
    document.querySelector("#log-next").disabled = logState.loading || (
      liveMode ? !logState.hasNext : logState.page === pageCount
    );
    if (liveMode && !logState.loaded) {
      tbody.innerHTML = `<tr><td colspan="5">記録を読み込んでいます。</td></tr>`;
      return;
    }
    if (events.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5">条件に一致する記録はありません。</td></tr>`;
      return;
    }
    tbody.innerHTML = pageEvents.map((event) => {
      const timestamp = formatLocalDateTime(eventTimeMs(event), true);
      const displayTimestamp = timestamp.slice(5, 19).replace("-", "/");
      return `
      <tr>
        <td data-label="日時" title="${escapeHtml(timestamp)}">${escapeHtml(displayTimestamp)}</td>
        <td data-label="信号"><strong>${escapeHtml(signalDisplayName(event.signal))}</strong> <span class="channel-tag${event.signal === "IN2" ? " channel-tag--alarm" : ""}">${escapeHtml(event.signal)}</span></td>
        <td data-label="変化"><span class="value-pill value-pill--${escapeHtml(event.value.toLowerCase())}">${escapeHtml(event.value)}</span></td>
        <td data-label="記録の種類"><span class="kind-label">${escapeHtml(kindLabel(event))}</span></td>
        <td data-label="時刻"><span class="${event.synced ? "sync-ok" : "sync-warning"}">${event.synced ? "正常" : "要確認"}</span></td>
      </tr>
    `;
    }).join("");
  }

  /**
   * 画面の検索条件からAPI query parameterを組み立てる。
   * @param {boolean} includePaging - paging queryを含めるか。
   * @returns {string} 現在のfilter stateを使って組み立てたquery文字列。
   */
  function logApiQuery(includePaging) {
    const periodKey = document.querySelector("#log-period").value;
    return buildLogApiQuery({
      periodKey,
      customBounds: periodKey === "custom" ? periodBounds(periodKey) : null,
      logSearchRange: logState.searchRange,
      selectedSignal: document.querySelector("#log-signal").value,
      widgetLogSignals,
      selectedValue: document.querySelector("#log-value").value,
      selectedKind: document.querySelector("#log-kind").value,
      selectedSource: document.querySelector("#log-source").value,
      includePaging,
      pageSize: includePaging ? Number(document.querySelector("#log-page-size").value) : null,
      logPage: logState.page
    });
  }

  /**
   * 現在の検索条件に合うlive log pageを取得する。
   * @returns {Promise<void>} APIからpageを取得してlogs viewを更新後resolveする。transport/API失敗はrejectする。
   */
  async function loadLiveLogPage() {
    await logState.loadPage({
      fetchPage: () => liveJson(`/api/logs?${logApiQuery(true)}`),
      pageSize: () => Number(document.querySelector("#log-page-size").value),
      now: () => Date.now(),
      onChange: renderLogs
    });
  }

  /**
   * ログ条件を再読込し、最初のpageから表示を更新する。
   * @returns {Promise<void>} page 1の検索を再実行し、結果をDOMへ描画後resolveする。
   */
  function reloadLogQuery() {
    // フィルター変更後は新たな時間範囲を検索する。ページ送りの間だけ範囲を固定する。
    logState.resetQuery();
    if (liveMode) {
      loadLiveLogPage().catch((error) => showToast(error.message));
    } else {
      renderLogs();
    }
  }

  /**
   * ログ・診断などのデータ画面を更新する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderDataViews() {
    renderLogSignalOptions();
    renderRecentEvents();
    renderTimeline();
    renderLogs();
    renderDashboardPeriod();
    renderCustomHistoryWidgets();
  }

  /**
   * 入力channel設定を画面へ描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderChannelSettings() {
    document.querySelector("#channel-settings").innerHTML = fixtures.channels.map((channel) => `
      <div class="channel-row${channel.enabled ? "" : " is-disabled"}" data-channel="${channel.id}">
        <div class="channel-identity"><strong>IN${channel.id}</strong><small>${channel.enabled ? channel.value : "未使用"}</small></div>
        <label>表示名<input class="channel-name" value="${escapeHtml(channel.name)}" aria-label="IN${channel.id}の表示名" maxlength="40"></label>
        <label>信号確定待ち時間（ms）<input class="channel-debounce" type="number" min="0" max="5000" step="1" value="${channel.debounce}" aria-label="IN${channel.id}の信号確定待ち時間（ms）"></label>
        <div class="settings-field channel-use"><span class="field-label">使用</span><button class="switch${channel.enabled ? " is-on" : ""}" type="button" role="switch" aria-checked="${channel.enabled}" aria-label="IN${channel.id}を使用" data-on-label="使用中" data-off-label="未使用"><span></span></button></div>
      </div>
    `).join("");
  }

  /**
   * 初回設定wizardを現在のsetup stateで描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderSetupWizard() {
    setSwitchState(document.querySelector("#setup-password-protection"), fixtures.settings.passwordProtection);
    document.querySelector("#setup-password-field").hidden = !fixtures.settings.passwordProtection;
    document.querySelector("#setup-channel-list").innerHTML = fixtures.channels.map((channel) => `
      <div class="setup-channel-row" data-setup-channel="${channel.id}">
        <button class="switch${channel.enabled ? " is-on" : ""}" type="button" role="switch" aria-checked="${channel.enabled}" aria-label="IN${channel.id}を使用" data-on-label="使用中" data-off-label="未使用"><span></span></button>
        <label>名称<input class="setup-channel-name" value="${escapeHtml(channel.name)}" maxlength="40"></label>
        <label>確定待ち時間（ms）<input class="setup-channel-debounce" type="number" min="0" max="5000" step="1" value="${channel.debounce}" aria-label="IN${channel.id}の信号確定待ち時間（ms）"></label>
      </div>
    `).join("");
    if (!fixtures.settings.setupCompleted || new URLSearchParams(window.location.search).get("setup") === "1") {
      document.querySelector("#setup-dialog").showModal();
    }
  }

  /**
   * 初回設定入力をまとめて保存する。
   * @returns {Promise<void>} setup payloadを保存し、成功時は完了画面へ遷移する。API失敗はrejectする。
   */
  async function completeSetup() {
    // 保存前に画面入力を正規化して一括検証し、field単位の部分送信を避ける。
    // API側でも同じ制約を再検証するが、ここでは即時の入力案内を担う。
    const deviceName = document.querySelector("#setup-device-name").value.trim();
    const passwordProtection = switchIsOn(document.querySelector("#setup-password-protection"));
    const error = document.querySelector("#setup-error");
    const channels = [...document.querySelectorAll("[data-setup-channel]")].map((row) => {
      const debounceValue = row.querySelector(".setup-channel-debounce").value.trim();
      return {
        id: Number(row.dataset.setupChannel),
        enabled: switchIsOn(row.querySelector(".switch")),
        name: row.querySelector(".setup-channel-name").value.trim(),
        debounce_ms: debounceValue === "" ? Number.NaN : Number(debounceValue),
        on_label: "ON",
        off_label: "OFF"
      };
    });
    const invalidDebounce = channels.find((channel) => !Number.isInteger(channel.debounce_ms) || channel.debounce_ms < 0 || channel.debounce_ms > 5000);
    let message = "";
    if (!deviceName) message = "設備名を入力してください。";
    else if (passwordProtection && !document.querySelector("#setup-password").value) message = "共有パスワードを入力してください。";
    else if (invalidDebounce) message = `IN${invalidDebounce.id}の信号確定待ち時間は0〜5000msの整数で入力してください。`;
    if (message) {
      error.textContent = message;
      error.hidden = false;
      return;
    }
    if (liveMode) {
      try {
        await liveJson("/api/setup", {
          method: "POST",
          body: JSON.stringify({
            device_name: deviceName,
            timezone: document.querySelector("#setup-timezone").value,
            retention_days: retentionDaysFromSelectValue(document.querySelector("#setup-retention").value),
            password_protection: passwordProtection,
            password: passwordProtection ? document.querySelector("#setup-password").value : null,
            channels
          })
        });
        if (passwordProtection && !fixtures.settings.passwordProtection) {
          // 初回設定で認証を初めて有効にすると、直後の再読込APIは未認証Cookieで401になる。
          // 設定保存後は保護されたAPIを呼ばず、共有パスワードのログインへ移動する。
          window.location.replace("/login");
          return;
        }
        await refreshLiveState();
        // refreshLiveStateは編集中の設定フォームを保護するため値を保持する。
        // 初回設定直後は未設定時の表示値が残るので、保存済み値で明示的に同期する。
        renderOutputSettings();
        document.querySelector("#setup-dialog").close();
        showToast("初回設定を完了し、記録を開始しました");
      } catch (error) {
        const setupError = document.querySelector("#setup-error");
        setupError.textContent = error instanceof Error ? error.message : "初回設定に失敗しました";
        setupError.hidden = false;
      }
      return;
    }
    document.querySelectorAll("[data-setup-channel]").forEach((row) => {
      const channel = fixtures.channels.find((item) => item.id === Number(row.dataset.setupChannel));
      channel.enabled = switchIsOn(row.querySelector(".switch"));
      channel.name = row.querySelector(".setup-channel-name").value.trim();
      channel.debounce = channels.find((item) => item.id === channel.id).debounce_ms;
    });
    fixtures.settings.deviceName = deviceName;
    fixtures.settings.timezone = document.querySelector("#setup-timezone").value;
    fixtures.settings.passwordProtection = passwordProtection;
    fixtures.settings.retentionDays = retentionDaysFromSelectValue(document.querySelector("#setup-retention").value);
    fixtures.settings.setupCompleted = true;
    document.querySelector("#page-title").textContent = deviceName;
    renderChannelSettings();
    renderClock();
    renderOutputSettings();
    renderLogSignalOptions();
    renderHealth();
    refreshCurrentStateCards();
    renderDataViews();
    document.querySelector("#setup-dialog").close();
    showToast("初回設定を完了し、記録を開始しました");
  }

  /**
   * 診断状態を画面へ描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderHealth() {
    const heartbeatHealth = fixtures.health.find((item) => item.label === "Heartbeat状態");
    const heartbeat = fixtures.settings.heartbeat;
    const critical = fixtures.loggerStatus === "critical";
    const loggerStateKnown = ["logging", "maintenance", "critical"].includes(fixtures.loggerStatus);
    heartbeatHealth.value = heartbeat.enabled
      ? (logging
        ? `OUT${heartbeat.outputId} · ${heartbeat.intervalSeconds}秒`
        : critical ? "停止中（Critical）"
          : loggerStateKnown ? "停止中（メンテナンス）" : "状態不明")
      : "無効";
    heartbeatHealth.state = critical
      ? "critical"
      : heartbeat.enabled && logging ? "ok" : "warning";
    document.querySelector("#health-items").innerHTML = fixtures.health.map((item) => `
      <div class="health-item"><span>${escapeHtml(item.label)}</span><strong${item.id ? ` id="${escapeHtml(item.id)}"` : ""} class="health-value ${escapeHtml(item.state)}">${escapeHtml(item.value)}</strong></div>
    `).join("");
    const allIo = [
      ...fixtures.channels.map((item) => ({ label: item.name ? `IN${item.id} ${item.name}` : `IN${item.id}`, value: item.enabled ? (logging ? item.value : "UNKNOWN") : "未使用" })),
      ...fixtures.outputs.map((item) => ({ label: `OUT${item.id}`, value: item.value }))
    ];
    document.querySelector("#io-grid").innerHTML = allIo.map((item) => `
      <div class="io-cell is-${item.value === "未使用" ? "unknown" : escapeHtml(item.value.toLowerCase())}"><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.value)}</small></div>
    `).join("");
    renderClock();
  }

  /**
   * OUT testの状態と操作を画面へ描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderOutputTests() {
    const critical = fixtures.loggerStatus === "critical";
    const configurationBlocked = Boolean(fixtures.configurationSync?.outOfSync);
    const loggerStateKnown = ["logging", "maintenance", "critical"].includes(fixtures.loggerStatus);
    document.querySelector("#output-tests").innerHTML = fixtures.outputs.map((output) => {
      const activeTest = activeOutputTest?.outputId === output.id;
      const disabled = !activeTest && (
        critical
        || !loggerStateKnown
        || configurationBlocked
        || logging
        || Boolean(activeOutputTest)
      );
      return `
        <button class="test-button${activeTest ? " is-testing" : ""}" data-output="${output.id}" type="button" ${disabled ? "disabled" : ""}>${activeTest ? `OUT${output.id} テスト終了` : `OUT${output.id} テスト`}</button>
      `;
    }).join("");
  }

  /**
   * 表示中のOUT testを終了状態にする。
   * @param {boolean} showMessage - testを通常終了したsuccess通知を表示するか。
   * @returns {Promise<void>} end-test要求後にtest state表示を更新する。API失敗時はtoast通知する。
   */
  async function finishOutputTest(showMessage = true) {
    if (!activeOutputTest) return;
    if (liveMode) {
      // timer満了と利用者の明示終了は同じend endpointを通す。画面側でOFFを
      // 推測せず、ACK後にstatus snapshotを読んで表示を揃える。
      try {
        await liveJson("/api/outputs/test/end", { method: "POST" });
        activeOutputTest = null;
        await refreshLiveState();
        if (showMessage) showToast("出力テストを終了しました");
      } catch (error) {
        await refreshLiveState().catch(() => {});
        showToast(error instanceof Error ? error.message : "出力テストの終了に失敗しました");
      }
      return;
    }
    window.clearTimeout(activeOutputTest.timerId);
    const outputId = activeOutputTest.outputId;
    const output = fixtures.outputs.find((item) => item.id === outputId);
    output.value = "OFF";
    activeOutputTest = null;
    renderOutputTests();
    renderDashboardOutputs();
    renderHealth();
    if (showMessage) showToast(`OUT${outputId}をOFFに戻しました`);
  }

  /**
   * 選択OUTの許可された時間制限付きtestを要求する。
   * @param {number} outputId - 出力番号。
   * @returns {Promise<void>} 指定OUTのtest要求を送信し、ACKまたは失敗を画面へ反映する。
   */
  async function startOutputTest(outputId) {
    if (liveMode) {
      // 5秒上限の実機側制約を明示して要求し、受付後だけtimerを開始する。
      // 失敗時は状態を再取得して、画面のON表示が残らないようにする。
      try {
        await liveJson("/api/outputs/test", {
          method: "POST",
          body: JSON.stringify({ channel: outputId, duration_ms: 5000 })
        });
        activeOutputTest = { outputId, timerId: window.setTimeout(() => finishOutputTest(), 5000) };
        await refreshLiveState();
      } catch (error) {
        await refreshLiveState().catch(() => {});
        showToast(error instanceof Error ? error.message : "出力テストを開始できませんでした");
      }
      return;
    }
    const output = fixtures.outputs.find((item) => item.id === outputId);
    output.value = "ON";
    activeOutputTest = { outputId, timerId: null };
    activeOutputTest.timerId = window.setTimeout(() => finishOutputTest(), 5000);
    renderOutputTests();
    renderDashboardOutputs();
    renderHealth();
  }

  /**
   * 各OUTの唯一のowner設定を画面へ反映する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function updateOutputOwners() {
    fixtures.outputs.forEach((output) => {
      output.owner = "未使用";
      output.name = "未使用";
      if (!activeOutputTest || activeOutputTest.outputId !== output.id) output.value = "OFF";
    });
    const heartbeat = fixtures.settings.heartbeat;
    if (heartbeat.enabled) {
      const output = fixtures.outputs.find((item) => item.id === heartbeat.outputId);
      output.owner = "Heartbeat";
      output.name = "Heartbeat";
      if (logging) output.value = "ON";
    }
    fixtures.outputRules.forEach((rule) => {
      const output = fixtures.outputs.find((item) => item.id === rule.outputId);
      output.owner = "条件ルール";
      output.name = rule.name;
    });
    if (
      liveMode
      && !["logging", "maintenance", "critical"].includes(fixtures.loggerStatus)
    ) {
      fixtures.outputs.forEach((output) => { output.value = "UNKNOWN"; });
    }
  }

  /**
   * ruleの時間条件を画面用文字列へ変換する。
   * @param {string} rule - 条件出力rule設定。
   * @returns {string} ruleの継続時間条件を表示するlabel。
   */
  function durationLabel(rule) {
    const units = { seconds: "秒", minutes: "分", hours: "時間" };
    return `${rule.duration}${units[rule.durationUnit]}`;
  }

  /**
   * 条件出力ruleを人が読める説明にする。
   * @param {string} rule - 条件出力rule設定。
   * @returns {string} rule kindと条件値をまとめた説明文。
   */
  function ruleDescription(rule) {
    const name = signalDisplayName(rule.channelId);
    const signalLabel = name === rule.channelId ? rule.channelId : `${rule.channelId} ${name}`;
    if (rule.condition === "event_absence") {
      const edge = rule.edge === "ON" ? "OFF→ON" : "ON→OFF";
      return `${signalLabel}の${edge}が${durationLabel(rule)}発生しない`;
    }
    return `${signalLabel}が${rule.value}のまま${durationLabel(rule)}継続`;
  }

  /**
   * Heartbeat設定項目の有効状態と値を同期する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function syncHeartbeatFields() {
    const enabled = switchIsOn(document.querySelector("#heartbeat-enabled"));
    document.querySelector("#heartbeat-output").disabled = !enabled;
    document.querySelector("#heartbeat-interval").disabled = !enabled;
  }

  /**
   * OUTと条件ruleの設定を画面へ描画する。
   * @param {boolean} preserveForm - 保存後も既存の編集form内容を保持するか。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderOutputSettings(preserveForm = false) {
    const heartbeat = fixtures.settings.heartbeat;
    if (!preserveForm) {
      setSwitchState(document.querySelector("#heartbeat-enabled"), heartbeat.enabled);
      document.querySelector("#heartbeat-output").innerHTML = fixtures.outputs.map((output) => (
        `<option value="${output.id}">OUT${output.id}</option>`
      )).join("");
      document.querySelector("#heartbeat-output").value = String(heartbeat.outputId);
      document.querySelector("#heartbeat-interval").value = String(heartbeat.intervalSeconds);
      document.querySelector("#retention-days").value = fixtures.settings.retentionDays == null
        ? ""
        : String(fixtures.settings.retentionDays);
      document.querySelector("#device-name").value = fixtures.settings.deviceName;
      document.querySelector("#hostname").value = fixtures.settings.hostname;
      document.querySelector("#timezone").value = fixtures.settings.timezone;
      setSwitchState(document.querySelector("#password-protection"), fixtures.settings.passwordProtection);
      document.querySelector("#shared-password-field").hidden = !fixtures.settings.passwordProtection;
    }
    syncHeartbeatFields();
    const list = document.querySelector("#output-rule-list");
    list.innerHTML = fixtures.outputRules.map((rule) => {
      const channelEnabled = fixtures.channels.find((channel) => `IN${channel.id}` === rule.channelId)?.enabled;
      const paused = rule.enabled && !channelEnabled;
      return `
      <div class="rule-row" data-rule-id="${rule.id}">
        <div><strong>${escapeHtml(rule.name)}</strong><small>${escapeHtml(ruleDescription(rule))}${paused ? "・入力無効のため一時停止" : ""}</small></div>
        <span>OUT${rule.outputId}</span>
        <button class="switch${rule.enabled ? " is-on" : ""}" type="button" role="switch" aria-checked="${rule.enabled}" aria-label="${escapeHtml(rule.name)}を有効にする" data-rule-enabled="${rule.id}" data-on-label="有効" data-off-label="無効"><span></span></button>
        <button class="secondary-button small-button" type="button" data-edit-rule="${rule.id}">編集</button>
      </div>
    `;
    }).join("") || '<p class="empty-rule-list">条件ルールはありません。</p>';
  }

  /**
   * ruleの有効状態を切り替えて保存する。
   * @param {number} ruleId - rule ID。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  async function toggleOutputRule(ruleId) {
    const rule = fixtures.outputRules.find((item) => item.id === ruleId);
    if (!rule) return;
    const enabled = !rule.enabled;
    if (enabled) {
      const channel = fixtures.channels.find((item) => `IN${item.id}` === rule.channelId);
      if (!channel?.enabled) {
        showToast(`${rule.channelId}が無効です。先に入力を有効にしてください`);
        return;
      }
      const conflict = outputConflict(rule.outputId, rule.id);
      if (conflict) {
        showToast(`OUT${rule.outputId}は${conflict}で使用中です`);
        return;
      }
    }
    if (liveMode) {
      try {
        const unitMs = { seconds: 1000, minutes: 60_000, hours: 3_600_000 };
        await liveJson(`/api/output-rules/${rule.id}`, {
          method: "PUT",
          body: JSON.stringify({
            name: rule.name,
            condition: rule.condition,
            input_channel: Number(rule.channelId.replace("IN", "")),
            output_channel: rule.outputId,
            duration_ms: rule.duration * unitMs[rule.durationUnit],
            value: rule.value === "ON",
            edge: rule.edge === "ON" ? "rising" : "falling",
            enabled
          })
        });
        await refreshLiveState();
        showToast(enabled ? `${rule.name}を有効にしました` : `${rule.name}を無効にしました`);
      } catch (error) {
        showToast(error instanceof Error ? error.message : "条件ルールを変更できませんでした");
      }
      return;
    }
    rule.enabled = enabled;
    updateOutputOwners();
    renderOutputSettings(true);
    renderLogSignalOptions();
    renderDashboardOutputs();
    renderHealth();
    showToast(enabled ? `${rule.name}を有効にしました` : `${rule.name}を無効にしました`);
  }

  /**
   * 入力channel selectにoptionを入れ、指定値を選択する。
   * @param {HTMLSelectElement} select - select element。
   * @param {string|number} selectedId - 初期選択ID。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function populateInputSelect(select, selectedId) {
    select.innerHTML = fixtures.channels.map((channel) => (
      `<option value="IN${channel.id}">${escapeHtml(channelLabel(channel))}</option>`
    )).join("");
    select.value = selectedId || "IN1";
  }

  /**
   * 選択したrule kindに応じた条件項目を表示する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function updateRuleConditionFields() {
    const absence = document.querySelector("#rule-condition").value === "event_absence";
    document.querySelector("#rule-value-field").hidden = absence;
    document.querySelector("#rule-edge-field").hidden = !absence;
  }

  /**
   * 新規または既存ruleの編集dialogを開く。
   * @param {number} ruleId - rule ID。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function openOutputRuleDialog(ruleId = null) {
    editingOutputRuleId = ruleId;
    const rule = fixtures.outputRules.find((item) => item.id === ruleId);
    document.querySelector("#output-rule-dialog-title").textContent = rule ? "条件ルールを編集" : "条件ルールを追加";
    document.querySelector("#rule-name").value = rule?.name || "";
    document.querySelector("#rule-condition").value = rule?.condition || "state_duration";
    populateInputSelect(document.querySelector("#rule-channel"), rule?.channelId || "IN1");
    document.querySelector("#rule-value").value = rule?.value || "OFF";
    document.querySelector("#rule-edge").value = rule?.edge || "ON";
    document.querySelector("#rule-duration").value = String(rule?.duration || 5);
    document.querySelector("#rule-duration-unit").value = rule?.durationUnit || "minutes";
    document.querySelector("#rule-output").innerHTML = fixtures.outputs.map((output) => `<option value="${output.id}">OUT${output.id}</option>`).join("");
    document.querySelector("#rule-output").value = String(rule?.outputId || 4);
    setSwitchState(document.querySelector("#rule-enabled"), rule?.enabled ?? true);
    document.querySelector("#delete-output-rule").hidden = !rule;
    const error = document.querySelector("#rule-error");
    error.hidden = true;
    error.textContent = "";
    updateRuleConditionFields();
    document.querySelector("#output-rule-dialog").showModal();
  }

  /**
   * 同じOUTを別ruleが所有する競合を判定する。
   * @param {number} outputId - 出力番号。
   * @param {number} ignoredRuleId - 除外するrule ID。
   * @returns {boolean} 指定OUTを別ruleがすでに所有する場合true。
   */
  function outputConflict(outputId, ignoredRuleId = null) {
    const heartbeat = fixtures.settings.heartbeat;
    if (heartbeat.enabled && heartbeat.outputId === outputId) return "Heartbeat";
    const rule = fixtures.outputRules.find((item) => item.outputId === outputId && item.id !== ignoredRuleId);
    return rule?.name || "";
  }

  /**
   * rule継続時間の入力が許可範囲か判定する。
   * @param {string} value - 変換対象の値。
   * @returns {boolean} rule durationが整数かつ実装で許可した範囲内ならtrue。
   */
  function validRuleDuration(value) {
    const duration = Number(value);
    return Number.isInteger(duration) && duration >= 1 && duration <= 86400;
  }

  /**
   * dialog入力を検証し、条件ruleをAPIへ保存する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  async function saveOutputRule() {
    const error = document.querySelector("#rule-error");
    const name = document.querySelector("#rule-name").value.trim();
    const channelId = document.querySelector("#rule-channel").value;
    const outputId = Number(document.querySelector("#rule-output").value);
    const duration = Number(document.querySelector("#rule-duration").value);
    const enabled = switchIsOn(document.querySelector("#rule-enabled"));
    const channel = fixtures.channels.find((item) => `IN${item.id}` === channelId);
    const conflict = outputConflict(outputId, editingOutputRuleId);
    let message = "";
    if (!name) message = "ルール名を入力してください。";
    else if (!validRuleDuration(duration)) message = "時間は1〜86400の整数で入力してください。";
    else if (enabled && !channel.enabled) message = `${channelId}が無効です。先に入力を有効にしてください。`;
    else if (conflict) message = `OUT${outputId}は${conflict}で使用中です。`;
    if (message) {
      error.textContent = message;
      error.hidden = false;
      return;
    }
    const values = {
      name,
      condition: document.querySelector("#rule-condition").value,
      channelId,
      value: document.querySelector("#rule-value").value,
      edge: document.querySelector("#rule-edge").value,
      duration,
      durationUnit: document.querySelector("#rule-duration-unit").value,
      outputId,
      enabled
    };
    const wasEditing = Boolean(editingOutputRuleId);
    if (liveMode) {
      try {
        const unitMs = { seconds: 1000, minutes: 60_000, hours: 3_600_000 };
        const body = {
          name,
          condition: values.condition,
          input_channel: Number(channelId.replace("IN", "")),
          output_channel: outputId,
          duration_ms: duration * unitMs[values.durationUnit],
          value: values.value === "ON",
          edge: values.edge === "ON" ? "rising" : "falling",
          enabled
        };
        const path = editingOutputRuleId === null
          ? "/api/output-rules"
          : `/api/output-rules/${editingOutputRuleId}`;
        await liveJson(path, { method: editingOutputRuleId === null ? "POST" : "PUT", body: JSON.stringify(body) });
        await refreshLiveState();
        document.querySelector("#output-rule-dialog").close();
        editingOutputRuleId = null;
        showToast(wasEditing ? "条件ルールを変更しました" : "条件ルールを追加しました");
      } catch (requestError) {
        error.textContent = requestError instanceof Error ? requestError.message : "条件ルールの保存に失敗しました";
        error.hidden = false;
      }
      return;
    }
    if (wasEditing) {
      Object.assign(fixtures.outputRules.find((item) => item.id === editingOutputRuleId), values);
    } else {
      const id = Math.max(0, ...fixtures.outputRules.map((item) => item.id)) + 1;
      fixtures.outputRules.push({ id, ...values });
    }
    updateOutputOwners();
    renderOutputSettings(true);
    renderLogSignalOptions();
    renderDashboardOutputs();
    renderHealth();
    renderDataViews();
    document.querySelector("#output-rule-dialog").close();
    showToast(wasEditing ? "条件ルールを変更しました" : "条件ルールを追加しました");
    editingOutputRuleId = null;
  }

  /**
   * loggerdへロギング開始・停止要求を送信する。
   * @param {string} nextLogging - 次にするロギング状態。
   * @returns {Promise<void>} start/stop要求のACK後にlive stateを再取得する。loggerd拒否・通信失敗はrejectする。
   */
  async function setLoggingState(nextLogging) {
    if (liveMode) {
      // live modeではdaemonのACKを唯一の状態根拠にする。停止/再開後もAPI snapshotを
      // 再読込してからmessageを出し、HTTP成功だけで記録状態を決めない。
      try {
        await liveJson(nextLogging ? "/api/logging/start" : "/api/logging/stop", { method: "POST" });
        await refreshLiveState();
        showToast(nextLogging ? "記録を再開しました" : "記録を停止してメンテナンス状態へ移行しました");
      } catch (error) {
        await refreshLiveState().catch(() => {});
        showToast(error instanceof Error ? error.message : "ロギング状態を変更できませんでした");
      }
      return;
    }
    // Demoではloggerd/DBがないため画面内の区間モデルを更新する。停止時はUNKNOWN
    // 区間を開始し、再開時は新しいsnapshotを先頭へ追加して状態境界を表現する。
    if (activeOutputTest) finishOutputTest(false);
    logging = nextLogging;
    if (logging) {
      loggingStartedAtMs = demoNowMs();
      const gap = unknownIntervals.at(-1);
      if (gap && !gap.endMs) gap.endMs = loggingStartedAtMs;
      loggingStoppedAtMs = null;
      const nextEventId = Math.max(0, ...fixtures.events.map((event) => event.id)) + 1;
      const snapshots = fixtures.channels.filter((channel) => channel.enabled).map((channel, index) => ({
        id: nextEventId + index,
        timestamp: new Date(loggingStartedAtMs).toISOString(),
        time: formatLocalDateTime(loggingStartedAtMs, true).slice(11, 23),
        signal: `IN${channel.id}`,
        value: channel.value,
        kind: "snapshot",
        source: "",
        synced: true
      }));
      fixtures.events.unshift(...snapshots.reverse());
    } else {
      loggingStoppedAtMs = demoNowMs();
      unknownIntervals.push({ startMs: loggingStoppedAtMs, endMs: null });
    }
    updateOutputOwners();
    if (!logging) fixtures.outputs.forEach((output) => { output.value = "OFF"; });
    const badge = document.querySelector("#logging-badge");
    const toggle = document.querySelector("#toggle-logging");
    const state = document.querySelector("#maintenance-state");
    badge.classList.toggle("is-stopped", !logging);
    badge.querySelector("span:last-child").textContent = logging ? "記録中" : "記録停止中";
    toggle.textContent = logging ? "記録を停止して出力テストへ" : "記録を再開";
    toggle.classList.toggle("danger-button", logging);
    toggle.classList.toggle("primary-button", !logging);
    state.innerHTML = logging
      ? `<span class="status-dot status-dot--ok"></span><div><strong>記録中</strong><small>入力記録・動作確認信号・出力ルールが動作中</small></div>`
      : `<span class="status-dot status-dot--stopped"></span><div><strong>記録停止中（メンテナンス）</strong><small>入力は記録されません。通常OUTはOFFです。設備の運転は停止していません。</small></div>`;
    document.querySelector("#maintenance-banner").hidden = logging;
    renderOutputTests();
    renderDashboardOutputs();
    renderHealth();
    refreshCurrentStateCards();
    renderDashboardPeriod();
    renderCustomHistoryWidgets();
    showToast(logging ? "記録を再開しました" : "記録を停止してメンテナンス状態へ移行しました");
  }

  /**
   * 利用者確認dialogを開き、確定後にactionを実行する。
   * @param {string} title - dialog title。
   * @param {string} message - 利用者向け説明。
   * @param {string} confirmLabel - 確認button文言。
   * @param {Function} action - 確認後のaction。
   * @returns {void} dialogへtitle/message/button文言を設定して開き、確定時にactionを呼び出す。
   */
  function openConfirmation(title, message, confirmLabel, action) {
    const dialog = document.querySelector("#confirm-dialog");
    document.querySelector("#dialog-title").textContent = title;
    document.querySelector("#dialog-message").textContent = message;
    document.querySelector("#dialog-confirm").textContent = confirmLabel;
    pendingAction = action;
    dialog.showModal();
  }

  /**
   * 現在の検索条件でCSVを取得し、downloadを開始する。
   * @returns {Promise<void>} CSV responseをbrowser downloadとして保存する。非成功responseではerror通知する。
   */
  function downloadCsv() {
    if (liveMode) {
      const anchor = document.createElement("a");
      anchor.href = `/api/logs.csv?${logApiQuery(false)}`;
      anchor.download = "events.csv";
      anchor.hidden = true;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      return;
    }
    const rows = filteredEvents();
    const text = serializeDemoCsv(rows, { formatLocalDateTime, eventTimeMs, signalDisplayName });
    const blob = new Blob(["\ufeff", text], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "event-log-demo.csv";
    anchor.click();
    URL.revokeObjectURL(url);
    showToast(`${rows.length}件のイベントを書き出しました`);
  }

  /**
   * 指定した設定sectionを表示する。
   * @param {string} sectionId - 設定section ID。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function openSettingsSection(sectionId) {
    switchView("settings");
    document.querySelectorAll(".settings-section").forEach((section) => {
      section.classList.toggle("is-active-settings", section.id === sectionId);
    });
    document.querySelectorAll("[data-settings-target]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.settingsTarget === sectionId);
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  /**
   * widget wizardの指定stepを表示する。
   * @param {number} step - wizard step番号。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function showWidgetStep(step) {
    currentWidgetStep = step;
    document.querySelectorAll("[data-widget-step]").forEach((panel) => {
      panel.classList.toggle("is-active", Number(panel.dataset.widgetStep) === step);
    });
    document.querySelector("#widget-step-count").textContent = `${step} / 3`;
    document.querySelector("#widget-back").classList.toggle("is-hidden", step === 1);
    document.querySelector("#widget-next").classList.toggle("is-hidden", step === 3);
    document.querySelector("#confirm-add-widget").classList.toggle("is-hidden", step !== 3);
  }

  /**
   * groupに適用可能なwidget type選択肢を設定する。
   * @param {string} group - widget group。
   * @param {string} selectedType - 選択type。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function setWidgetTypeOptions(group, selectedType) {
    const types = group === "realtime"
      ? ["現在状態"]
      : ["イベント回数", "ON/OFF時間", "最終イベント", "経過時間", "タイムライン", "イベント一覧"];
    const typeSelect = document.querySelector("#new-widget-type");
    typeSelect.innerHTML = types.map((type) => `<option>${type}</option>`).join("");
    typeSelect.value = types.includes(selectedType) ? selectedType : types[0];
    document.querySelector("#widget-type-help").textContent = widgetTypeHelp[typeSelect.value];
  }

  /**
   * widget用channel選択肢を描画する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function renderWidgetChannelOptions() {
    const select = document.querySelector("#new-widget-channel");
    select.innerHTML = fixtures.channels.map((channel) => (
      `<option value="IN${channel.id}">${escapeHtml(channelLabel(channel))}</option>`
    )).join("");
    const inputs = fixtures.channels.map((channel) => `
      <label><input type="checkbox" value="IN${channel.id}">${escapeHtml(channelLabel(channel))}</label>
    `);
    const outputs = fixtures.outputs.map((output) => `
      <label><input type="checkbox" value="OUT${output.id}">OUT${output.id} ${escapeHtml(output.name)}</label>
    `);
    document.querySelector("#widget-channel-checks").innerHTML = [...inputs, ...outputs].join("");
  }

  /**
   * 選択channel群を画面状態へ設定する。
   * @param {string} value - 変換対象の値。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function setWidgetChannels(value) {
    const ids = signalIds(value);
    document.querySelector("#new-widget-channel").value = ids[0] || "IN1";
    document.querySelectorAll("#widget-channel-checks input").forEach((input) => {
      input.checked = ids.includes(input.value);
    });
  }

  /**
   * 現在選択されているwidget channel群を返す。
   * @returns {Array<string>} widget画面で現在選択中のIN/OUT signal ID列。
   */
  function selectedWidgetChannels() {
    const type = document.querySelector("#new-widget-type").value;
    if (!["タイムライン", "イベント一覧"].includes(type)) return document.querySelector("#new-widget-channel").value;
    const checked = [...document.querySelectorAll("#widget-channel-checks input:checked")].map((input) => input.value);
    return checked.join(",");
  }

  /**
   * widget追加・編集dialogを初期化して開く。
   * @param {boolean} isEditing - 編集modeか。
   * @param {string} widgetGroup - widget group。
   * @returns {void} 選択mode/groupに合わせてfieldを初期化し、modalを開く。
   */
  function openWidgetDialog(isEditing, widgetGroup = "realtime") {
    const typeSelect = document.querySelector("#new-widget-type");
    typeSelect.disabled = isEditing;
    renderWidgetChannelOptions();
    if (isEditing && widgetEditor.editingRow) {
      addingWidgetGroup = widgetEditor.editingRow.closest(".widget-group-list").dataset.widgetGroup;
      setWidgetTypeOptions(addingWidgetGroup, widgetTypeForRow(widgetEditor.editingRow));
      setWidgetChannels(widgetChannelForRow(widgetEditor.editingRow));
      document.querySelector("#new-widget-on-label").value = widgetEditor.editingRow.dataset.onLabel || "ON";
      document.querySelector("#new-widget-off-label").value = widgetEditor.editingRow.dataset.offLabel || "OFF";
      setSwitchState(document.querySelector("#new-widget-show-elapsed"), widgetEditor.editingRow.dataset.showElapsed !== "false");
      document.querySelector("#new-widget-period").value = widgetEditor.editingRow.dataset.widgetPeriod || "follow";
      document.querySelector("#new-widget-edge").value = widgetEditor.editingRow.dataset.widgetEdge || "ON";
      document.querySelector("#new-widget-state").value = widgetEditor.editingRow.dataset.widgetState || "ON";
      document.querySelector("#new-widget-source").value = widgetEditor.editingRow.dataset.widgetSource || "state";
      document.querySelector("#new-widget-count").value = widgetEditor.editingRow.dataset.widgetCount || "10";
      document.querySelector("#new-widget-value").value = widgetEditor.editingRow.dataset.widgetValue || "all";
      document.querySelector("#new-widget-count-mode").value = widgetEditor.editingRow.dataset.widgetCountMode || "period";
      document.querySelector("#new-widget-unit").value = widgetEditor.editingRow.dataset.widgetUnit || "回";
      document.querySelector("#new-widget-time-unit").value = widgetEditor.editingRow.dataset.widgetTimeUnit || "human";
      setSwitchState(document.querySelector("#new-widget-show-absolute"), widgetEditor.editingRow.dataset.showAbsolute !== "false");
      setSwitchState(document.querySelector("#new-widget-show-event-elapsed"), widgetEditor.editingRow.dataset.showEventElapsed === "true");
    } else {
      addingWidgetGroup = widgetGroup;
      setWidgetTypeOptions(addingWidgetGroup, addingWidgetGroup === "realtime" ? "現在状態" : "イベント回数");
      setWidgetChannels("IN1");
      document.querySelector("#new-widget-on-label").value = "ON";
      document.querySelector("#new-widget-off-label").value = "OFF";
      setSwitchState(document.querySelector("#new-widget-show-elapsed"), true);
      document.querySelector("#new-widget-period").value = "follow";
      document.querySelector("#new-widget-edge").value = "ON";
      document.querySelector("#new-widget-state").value = "ON";
      document.querySelector("#new-widget-source").value = "state";
      document.querySelector("#new-widget-count").value = "10";
      document.querySelector("#new-widget-value").value = "all";
      document.querySelector("#new-widget-count-mode").value = "period";
      document.querySelector("#new-widget-unit").value = "回";
      document.querySelector("#new-widget-time-unit").value = "human";
      setSwitchState(document.querySelector("#new-widget-show-absolute"), true);
      setSwitchState(document.querySelector("#new-widget-show-event-elapsed"), false);
    }
    document.querySelector("#widget-detail-options").open = false;
    updateWidgetTypeFields();
    const groupLabel = addingWidgetGroup === "realtime" ? "リアルタイム" : "集計・履歴";
    document.querySelector("#widget-dialog-title").textContent = isEditing ? "表示項目を編集" : `${groupLabel}の表示項目を追加`;
    document.querySelector("#confirm-add-widget").textContent = isEditing ? "変更" : "追加";
    showWidgetStep(1);
    document.querySelector("#widget-dialog").showModal();
  }

  /**
   * widget typeに応じて編集fieldの表示を切り替える。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function updateWidgetTypeFields() {
    const type = document.querySelector("#new-widget-type").value;
    const isCurrentState = type === "現在状態";
    const isMultiSignal = ["タイムライン", "イベント一覧"].includes(type);
    const isElapsedTime = type === "経過時間";
    const elapsedSource = document.querySelector("#new-widget-source").value;
    document.querySelector("#current-state-options").hidden = !isCurrentState;
    const countWithoutPeriod = type === "イベント回数" && document.querySelector("#new-widget-count-mode").value !== "period";
    document.querySelector("#widget-period-field").hidden = isCurrentState || countWithoutPeriod;
    document.querySelector("#widget-single-channel-field").hidden = isMultiSignal;
    document.querySelector("#widget-multi-channel-field").hidden = !isMultiSignal;
    document.querySelector("#widget-elapsed-source-field").hidden = !isElapsedTime;
    const elapsedUsesEvent = isElapsedTime && elapsedSource === "event";
    document.querySelector("#widget-edge-label").textContent = elapsedUsesEvent ? "起点イベント" : "数える変化";
    document.querySelector("#widget-edge-field").hidden = !["イベント回数", "最終イベント"].includes(type) && !elapsedUsesEvent;
    document.querySelector("#widget-state-label").textContent = type === "ON/OFF時間" ? "合計する状態" : "対象状態";
    document.querySelector("#widget-state-field").hidden = type !== "ON/OFF時間" && !(isElapsedTime && elapsedSource === "state");
    document.querySelector("#widget-count-fields").hidden = type !== "イベント回数";
    document.querySelector("#widget-time-unit-field").hidden = type !== "ON/OFF時間";
    document.querySelector("#widget-last-event-fields").hidden = type !== "最終イベント";
    document.querySelector("#widget-list-fields").hidden = type !== "イベント一覧";
  }

  /**
   * widget順序に応じて上下移動buttonの状態を更新する。
   * @returns {void} DOM表示またはcontroller stateを更新する。値を返さない。
   */
  function updateMoveButtons() {
    document.querySelectorAll("#widget-settings-list .widget-group-list").forEach((group) => {
      const rows = [...group.querySelectorAll(".widget-setting-row")];
      rows.forEach((row, index) => {
        const upButton = row.querySelector(".move-up");
        const downButton = row.querySelector(".move-down");
        if (upButton) upButton.disabled = index === 0;
        if (downButton) downButton.disabled = index === rows.length - 1;
      });
    });
  }

  /**
   * 設定rowからwidget typeを読む。
   * @param {HTMLElement} row - widget row element。
   * @returns {string} widget setting rowが表すAPI/UI type。
   */
  function widgetTypeForRow(row) {
    const types = {
      "current-state": "現在状態",
      "cycle-count": "イベント回数",
      "on-time": "ON/OFF時間",
      "last-alarm": "最終イベント",
      timeline: "タイムライン",
      "recent-events": "イベント一覧"
    };
    return row.dataset.widgetType || types[row.dataset.widgetRow] || "現在状態";
  }

  /**
   * 設定rowから対象channelを読む。
   * @param {HTMLElement} row - widget row element。
   * @returns {string} widget setting rowから得たchannel選択値。
   */
  function widgetChannelForRow(row) {
    const channels = {
      "current-state": "IN1",
      "cycle-count": "IN3",
      "on-time": "IN1",
      "last-alarm": "IN2",
      timeline: "IN1,IN2,IN3,IN4,OUT1",
      "recent-events": "IN1,IN2,IN3,IN4,OUT1"
    };
    return row.dataset.widgetChannel || channels[row.dataset.widgetRow] || "IN1";
  }

  /**
   * 画面row値をAPI設定形式へまとめる。
   * @param {HTMLElement} row - widget row element。
   * @returns {Object} 保存APIへ渡すwidget種別別config object。
   */
  function widgetApiConfigForRow(row) {
    try {
      const config = JSON.parse(row.dataset.apiConfig || "{}");
      return config && typeof config === "object" && !Array.isArray(config) ? config : {};
    } catch {
      return {};
    }
  }

  /**
   * widget設定を一覧表示用の短い説明にする。
   * @param {string} type - widget type key。
   * @param {string} channelId - 製品上のIN/OUT番号。
   * @param {string} period - 期間選択値。
   * @param {Object} config - widget config。
   * @returns {string} type・channel・period・configから作る設定一覧向け説明。
   */
  function widgetSettingSummary(type, channelId, period, config = {}) {
    const periodText = period === "follow" ? "ダッシュボード期間に追従" : fixtures.dashboardPeriods[period].label;
    if (type === "現在状態") return `現在状態・${channelId}${config.showElapsed ? "・継続時間を表示" : ""}`;
    if (type === "イベント回数" && config.countMode === "since_reset") return `イベント回数・${channelId}・リセット後`;
    if (type === "イベント回数" && config.countMode === "stored_total") return `イベント回数・${channelId}・保存期間内累計`;
    if (type === "経過時間") {
      const origin = config.source === "event"
        ? `イベント${config.edge === "OFF" ? "ON→OFF" : "OFF→ON"}から`
        : `状態${config.state || "ON"}から`;
      return `経過時間・${channelId}・${origin}・${periodText}`;
    }
    if (type === "イベント一覧") return `イベント一覧・${channelId.replaceAll(",", "・")}・直近${config.count || 10}件`;
    return `${type}・${channelId.replaceAll(",", "・")}・${periodText}`;
  }

  /**
   * widget設定順・groupに合わせてDashboard layoutを同期する。
   * @returns {void} widget rowのgroup順に従ってDashboard cardをDOM上で並べ直す。
   */
  function syncDashboardLayout() {
    const dashboardTargets = {
      realtime: document.querySelector("#dashboard-realtime-grid"),
      history: document.querySelector("#dashboard-history-grid")
    };
    document.querySelectorAll("#widget-settings-list .widget-group-list").forEach((group) => {
      const dashboard = dashboardTargets[group.dataset.widgetGroup];
      group.querySelectorAll(".widget-setting-row").forEach((row) => {
        const widget = dashboardWidgetById(row.dataset.widgetRow);
        if (!widget) return;
        widget.dataset.size = row.dataset.dashboardSize;
        dashboard.append(widget);
      });
    });
    updateMoveButtons();
  }

  /**
   * IDに一致するDashboard widgetを検索する。
   * @param {string} widgetId - widget ID。
   * @returns {Object|null} ID一致widget。見つからない場合はnull。
   */
  function dashboardWidgetById(widgetId) {
    return [...document.querySelectorAll("#dashboard-realtime-grid [data-widget], #dashboard-history-grid [data-widget]")]
      .find((widget) => widget.dataset.widget === widgetId) || null;
  }

  /**
   * widget rowを同じgroup内で指定方向へ移動する。
   * @param {HTMLElement} row - widget row element。
   * @param {number} direction - 移動方向。
   * @returns {void} rowを同group内で上下へ移し、設定dirty stateを更新する。
   */
  function moveWidgetRow(row, direction) {
    const sibling = direction === "up" ? row.previousElementSibling : row.nextElementSibling;
    if (!sibling) return;
    if (direction === "up") {
      row.parentElement.insertBefore(row, sibling);
    } else {
      row.parentElement.insertBefore(sibling, row);
    }
    syncDashboardLayout();
    markWidgetSettingsDirty();
    row.focus({ preventScroll: true });
    showToast("表示順を変更しました");
  }

  /**
   * 既存IDと衝突しないwidget IDを生成する。
   * @param {string} prefix - ID prefix。
   * @returns {string} 指定prefixから生成した新しいwidget ID。
   */
  function uniqueWidgetId(prefix = "widget") {
    let number = 1;
    while (document.querySelector(`[data-widget="${prefix}-${number}"], [data-widget-row="${prefix}-${number}"]`)) number += 1;
    return `${prefix}-${number}`;
  }

  /**
   * rowからwidget設定を読み、overrideを適用する。
   * @param {HTMLElement} row - widget row element。
   * @param {string} overrides - 設定override値。
   * @returns {Object} row fieldとoverrideから作るwidget編集config。channelId、type、periodなど保存値を含む。
   */
  function widgetConfigFromRow(row, overrides = {}) {
    return {
      id: row.dataset.widgetRow,
      title: row.querySelector("strong").textContent,
      type: widgetTypeForRow(row),
      channelId: widgetChannelForRow(row),
      period: row.dataset.widgetPeriod || "follow",
      edge: row.dataset.widgetEdge || "ON",
      state: row.dataset.widgetState || "ON",
      count: Number(row.dataset.widgetCount || 10),
      value: row.dataset.widgetValue || "all",
      countMode: row.dataset.widgetCountMode || "period",
      unit: row.dataset.widgetUnit || "回",
      timeUnit: row.dataset.widgetTimeUnit || "human",
      showAbsolute: row.dataset.showAbsolute !== "false",
      showEventElapsed: row.dataset.showEventElapsed === "true",
      onLabel: row.dataset.onLabel || "ON",
      offLabel: row.dataset.offLabel || "OFF",
      showElapsed: row.dataset.showElapsed !== "false",
      source: row.dataset.widgetSource || "event",
      durationMs: Number(row.dataset.widgetDurationMs || 0),
      apiConfig: widgetApiConfigForRow(row),
      enabled: switchIsOn(row.querySelector(".switch")),
      size: row.dataset.dashboardSize || "small",
      ...overrides
    };
  }

  /**
   * widget設定を複製し、新しいIDと表示位置を割り当てる。
   * @param {HTMLElement} row - widget row element。
   * @returns {void} 現在rowの設定を複製し、新IDで保存する。
   */
  function duplicateWidget(row) {
    const group = row.closest(".widget-group-list").dataset.widgetGroup;
    const config = widgetConfigFromRow(row, {
      id: uniqueWidgetId(group === "realtime" ? "current-state" : "custom-widget"),
      title: `${row.querySelector("strong").textContent}（コピー）`
    });
    const list = row.closest(".widget-group-list");
    if (group === "realtime") {
      list.insertAdjacentHTML("beforeend", currentStateSettingRowHtml(config));
      document.querySelector("#dashboard-realtime-grid").insertAdjacentHTML("beforeend", currentStateCardHtml(config));
    } else {
      fixtures.counterResets[config.id] = [];
      list.insertAdjacentHTML("beforeend", historyWidgetSettingRowHtml(config));
      document.querySelector("#dashboard-history-grid").insertAdjacentHTML("beforeend", historyWidgetCardHtml(config));
    }
    syncDashboardLayout();
    markWidgetSettingsDirty();
    bindTimelineEvents();
    showToast("表示項目を複製しました");
  }

  /**
   * widget設定の削除確認を行い、確定後に削除する。
   * @param {HTMLElement} row - widget row element。
   * @returns {void} 確認dialog後に削除APIを呼び、widget一覧を更新する。
   */
  function deleteWidget(row) {
    const title = row.querySelector("strong").textContent;
    openConfirmation(
      "表示項目を削除しますか？",
      `「${title}」をダッシュボードから削除します。生ログは削除されません。`,
      "削除",
      () => {
        dashboardWidgetById(row.dataset.widgetRow)?.remove();
        delete fixtures.counterResets[row.dataset.widgetRow];
        row.remove();
        syncDashboardLayout();
        markWidgetSettingsDirty();
        showToast("表示項目を削除しました");
      }
    );
  }



  /**
   * 選択widgetのsignalと期間をログ検索へ引き継ぐ。
   * @param {Object} widget - widget設定。
   * @returns {void} widgetのsignalとperiod条件をログviewへ設定して表示を切り替える。
   */
  function openLogsForWidget(widget) {
    const period = periodKeyForWidget(widget.dataset.widgetPeriod || "follow");
    const periodSelect = document.querySelector("#log-period");
    periodSelect.value = [...periodSelect.options].some((option) => option.value === period) ? period : "8h";
    document.querySelector("#log-custom-period").hidden = periodSelect.value !== "custom";
    if (periodSelect.value === "custom") {
      document.querySelector("#log-custom-from").value = formatDateTimeInput(Date.parse(fixtures.dashboardPeriods.custom.startAt));
      document.querySelector("#log-custom-to").value = formatDateTimeInput(Date.parse(fixtures.dashboardPeriods.custom.endAt));
    }
    const ids = signalIds(widget.dataset.widgetChannel);
    widgetLogSignals = ids;
    renderLogSignalOptions();
    document.querySelector("#log-signal").value = ids.length === 1 ? ids[0] : "selection";
    document.querySelector("#log-value").value = logValueFilterForWidget(
      widget.dataset.widgetType,
      widget.dataset.widgetValue
    );
    document.querySelector("#log-kind").value = "all";
    document.querySelector("#log-source").value = "all";
    logState.resetQuery();
    renderLogs();
    switchView("logs");
  }

  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  document.querySelectorAll("[data-view-link]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.viewLink === "logs") {
        const widget = button.closest("[data-widget]");
        if (widget) openLogsForWidget(widget);
      }
      switchView(button.dataset.viewLink);
    });
  });
  document.querySelector("#dashboard-history-grid").addEventListener("click", (event) => {
    const miniZoomButton = event.target.closest("[data-mini-zoom]");
    if (miniZoomButton) {
      const card = miniZoomButton.closest("[data-custom-history-widget]");
      const currentIndex = Number(card.dataset.miniZoomIndex || 0);
      const nextIndex = miniZoomButton.dataset.miniZoom === "in"
        ? Math.min(timelineZoomLevels.length - 1, currentIndex + 1)
        : Math.max(0, currentIndex - 1);
      card.dataset.miniZoomIndex = String(nextIndex);
      applyMiniTimelineZoom(card);
      return;
    }
    const button = event.target.closest("[data-custom-log-link]");
    if (button) openLogsForWidget(button.closest("[data-widget]"));
    const resetButton = event.target.closest("[data-reset-counter]");
    if (resetButton) {
      const widget = resetButton.closest("[data-widget]");
      const currentValue = widget.querySelector(".summary-value strong, .custom-widget-value strong")?.textContent || "0";
      openConfirmation(
        "カウンタをリセットしますか？",
        `現在値：${currentValue}\nリセット後：0\n\n生ログは削除されません。`,
        "リセット",
        async () => {
          const widgetId = resetButton.dataset.resetCounter;
          if (liveMode) {
            try {
              await liveJson(`/api/widgets/${encodeURIComponent(widgetId)}/reset`, { method: "POST" });
              await refreshLiveState(true);
              showToast("カウンタをリセットしました");
            } catch (error) {
              showToast(error instanceof Error ? error.message : "カウンタをリセットできませんでした");
            }
            return;
          }
          fixtures.counterResets[widgetId] ||= [];
          const afterEventId = Math.max(0, ...fixtures.events.map((event) => event.id));
          const label = new Intl.DateTimeFormat("ja-JP", { timeZone: selectedTimeZone(), hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(demoNowMs()));
          fixtures.counterResets[widgetId].push({ afterEventId, carriedCount: 0, label });
          if (widget.matches("[data-custom-history-widget]")) renderHistoryWidgetCard(widget);
          else renderDashboardPeriod();
          showToast("カウンタをリセットしました");
        }
      );
    }
  });
  document.querySelectorAll(".period-button").forEach((button) => {
    button.addEventListener("click", async () => {
      const previousPeriod = currentPeriod;
      document.querySelectorAll(".period-button").forEach((item) => item.classList.remove("is-active"));
      button.classList.add("is-active");
      currentPeriod = button.dataset.period;
      document.querySelector("#dashboard-custom-period").hidden = currentPeriod !== "custom";
      renderDashboardPeriod();
      renderCustomHistoryWidgets();
      if (currentPeriod === "custom") {
        const customRange = {
          from_ms: parseLocalDateTime(document.querySelector("#dashboard-custom-from").value),
          to_ms: parseLocalDateTime(document.querySelector("#dashboard-custom-to").value)
        };
        dashboardCustomRangeDraft = (
          Number.isInteger(customRange.from_ms)
          && Number.isInteger(customRange.to_ms)
          && customRange.from_ms >= 0
          && customRange.from_ms < customRange.to_ms
        ) ? customRange : null;
        if (!dashboardCustomRangeDraft) {
          showToast("開始日時より後の終了日時を指定してください");
          return;
        }
        if (liveMode && dashboardCustomRangeDraft) {
          button.disabled = true;
          try {
            await refreshLiveState(true);
          } catch (error) {
            showToast(error instanceof Error ? error.message : "任意期間を読み込めませんでした");
          } finally {
            button.disabled = false;
          }
        }
        showToast(`表示期間を「${button.textContent}」に切り替えました`);
        return;
      }
      dashboardCustomRangeDraft = null;
      if (liveMode && currentPeriod !== "custom") {
        button.disabled = true;
        try {
          await liveJson("/api/settings/dashboard", {
            method: "POST",
            body: JSON.stringify({ period: currentPeriod })
          });
          fixtures.settings.dashboardPeriod = currentPeriod;
          await refreshLiveState(true);
        } catch (error) {
          currentPeriod = previousPeriod;
          document.querySelectorAll(".period-button").forEach((item) => {
            item.classList.toggle("is-active", item.dataset.period === currentPeriod);
          });
          document.querySelector("#dashboard-custom-period").hidden = currentPeriod !== "custom";
          renderDashboardPeriod();
          renderCustomHistoryWidgets();
          showToast(error instanceof Error ? error.message : "表示期間を保存できませんでした");
        } finally {
          button.disabled = false;
        }
        return;
      }
      showToast(`表示期間を「${button.textContent}」に切り替えました`);
    });
  });
  document.querySelector("#apply-dashboard-custom-period").addEventListener("click", () => {
    if (!applyCustomPeriod(document.querySelector("#dashboard-custom-from"), document.querySelector("#dashboard-custom-to"))) return;
    currentPeriod = "custom";
    renderDashboardPeriod();
    renderCustomHistoryWidgets();
    if (liveMode) {
      const customRange = {
        from_ms: parseLocalDateTime(document.querySelector("#dashboard-custom-from").value),
        to_ms: parseLocalDateTime(document.querySelector("#dashboard-custom-to").value)
      };
      dashboardCustomRangeDraft = customRange;
      liveJson("/api/settings/dashboard", {
        method: "POST",
        body: JSON.stringify({ period: "custom", ...customRange })
      }).then(async () => {
        fixtures.settings.dashboardPeriod = "custom";
        fixtures.settings.dashboardCustomRange = customRange;
        dashboardCustomRangeDraft = null;
        await refreshLiveState(true);
        showToast("任意期間を保存しました");
      }).catch((error) => {
        showToast(error instanceof Error ? error.message : "任意期間を保存できませんでした");
      });
      return;
    }
    showToast("任意期間を適用しました");
  });
  document.querySelector("#timeline-zoom-out").addEventListener("click", () => {
    timelineZoomIndex = Math.max(0, timelineZoomIndex - 1);
    applyTimelineZoom();
  });
  document.querySelector("#timeline-zoom-in").addEventListener("click", () => {
    timelineZoomIndex = Math.min(timelineZoomLevels.length - 1, timelineZoomIndex + 1);
    applyTimelineZoom();
  });
  document.addEventListener("click", (event) => {
    if (pinnedTimelineEvent && !event.target.closest("[data-timeline-event], #timeline-event-detail")) {
      hideTimelineDetail(true);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && pinnedTimelineEvent) hideTimelineDetail(true);
  });
  window.addEventListener("resize", () => {
    if (pinnedTimelineEvent) positionTimelineDetail(pinnedTimelineEvent);
  });
  document.querySelectorAll("#log-period, #log-signal, #log-value, #log-kind, #log-source").forEach((select) => {
      select.addEventListener("change", () => {
      if (select.id === "log-period") {
        document.querySelector("#log-custom-period").hidden = select.value !== "custom";
      }
      if (select.id === "log-source" && select.value !== "all") {
        document.querySelector("#log-kind").value = "output";
      }
      reloadLogQuery();
    });
  });
  document.querySelector("#apply-log-custom-period").addEventListener("click", () => {
    if (!applyCustomPeriod(document.querySelector("#log-custom-from"), document.querySelector("#log-custom-to"))) return;
    reloadLogQuery();
    showToast("任意期間を適用しました");
  });
  document.querySelector("#log-page-size").addEventListener("change", () => {
    reloadLogQuery();
  });
  document.querySelector("#log-prev").addEventListener("click", () => {
    logState.setPage(logState.page - 1);
    if (liveMode) loadLiveLogPage().catch((error) => showToast(error.message));
    else renderLogs();
  });
  document.querySelector("#log-next").addEventListener("click", () => {
    logState.setPage(logState.page + 1);
    if (liveMode) loadLiveLogPage().catch((error) => showToast(error.message));
    else renderLogs();
  });
  document.querySelector("#download-csv").addEventListener("click", downloadCsv);
  document.querySelector("#save-settings").addEventListener("click", async () => {
    const pendingChannels = [...document.querySelectorAll("#channel-settings .channel-row")].map((row) => {
      const debounceValue = row.querySelector(".channel-debounce").value.trim();
      return {
        id: Number(row.dataset.channel),
        name: row.querySelector(".channel-name").value.trim(),
        enabled: switchIsOn(row.querySelector(".switch")),
        debounce: debounceValue === "" ? Number.NaN : Number(debounceValue)
      };
    });
    const heartbeatEnabled = switchIsOn(document.querySelector("#heartbeat-enabled"));
    const heartbeatOutputId = Number(document.querySelector("#heartbeat-output").value);
    const intervalSeconds = Number(document.querySelector("#heartbeat-interval").value);
    const conflictingRule = fixtures.outputRules.find((rule) => rule.outputId === heartbeatOutputId);
    const invalidDebounce = pendingChannels.find((channel) => !Number.isInteger(channel.debounce) || channel.debounce < 0 || channel.debounce > 5000);
    const deviceName = document.querySelector("#device-name").value.trim();
    const hostname = document.querySelector("#hostname").value.trim();
    const passwordProtection = switchIsOn(document.querySelector("#password-protection"));
    if (invalidDebounce) {
      openSettingsSection("input-settings");
      showToast(`IN${invalidDebounce.id}の信号確定待ち時間は0〜5000msで入力してください`);
      return;
    }
    if (!deviceName || !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(hostname)) {
      openSettingsSection("system-settings");
      showToast(!deviceName ? "設備名を入力してください" : "ホスト名は英小文字・数字・ハイフンで入力してください");
      return;
    }
    if (passwordProtection && !fixtures.settings.passwordProtection && !document.querySelector("#shared-password").value) {
      openSettingsSection("system-settings");
      showToast("共有パスワードを入力してください");
      return;
    }
    if (heartbeatEnabled && (!Number.isInteger(intervalSeconds) || intervalSeconds < 1 || intervalSeconds > 60)) {
      openSettingsSection("output-settings");
      document.querySelector("#heartbeat-interval").focus();
      showToast("Heartbeatの切替間隔は1〜60秒で入力してください");
      return;
    }
    if (heartbeatEnabled && conflictingRule) {
      openSettingsSection("output-settings");
      document.querySelector("#heartbeat-output").focus();
      showToast(`OUT${heartbeatOutputId}は${conflictingRule.name}で使用中です`);
      return;
    }
    if (liveMode) {
      try {
        for (const pending of pendingChannels) {
          await liveJson(`/api/channels/${pending.id}`, {
            method: "POST",
            body: JSON.stringify({
              name: pending.name,
              enabled: pending.enabled,
              debounce_ms: pending.debounce,
              on_label: "ON",
              off_label: "OFF"
            })
          });
        }
        await liveJson("/api/settings/heartbeat", {
          method: "POST",
          body: JSON.stringify({
            enabled: heartbeatEnabled,
            channel_id: heartbeatOutputId,
            interval_ms: intervalSeconds * 1000
          })
        });
        await liveJson("/api/settings/retention", {
          method: "POST",
          body: JSON.stringify({ retention_days: retentionDaysFromSelectValue(document.querySelector("#retention-days").value) })
        });
        // 認証変更で旧Cookieが失効するため、保留中の表示設定を先に保存する。
        await saveWidgetSettings();
        await liveJson("/api/settings/system", {
          method: "POST",
          body: JSON.stringify({
            device_name: deviceName,
            hostname,
            timezone: document.querySelector("#timezone").value,
            password_protection: passwordProtection,
            password: document.querySelector("#shared-password").value || null
          })
        });
        await refreshLiveState();
        showToast("設定を保存しました");
      } catch (error) {
        showToast(error instanceof Error ? error.message : "設定の保存に失敗しました");
      }
      return;
    }
    pendingChannels.forEach((pending) => {
      Object.assign(fixtures.channels.find((channel) => channel.id === pending.id), pending);
    });
    fixtures.settings.heartbeat = { enabled: heartbeatEnabled, outputId: heartbeatOutputId, intervalSeconds };
    fixtures.settings.retentionDays = retentionDaysFromSelectValue(document.querySelector("#retention-days").value);
    fixtures.settings.deviceName = deviceName;
    fixtures.settings.hostname = hostname;
    fixtures.settings.timezone = document.querySelector("#timezone").value;
    fixtures.settings.passwordProtection = passwordProtection;
    document.querySelector("#page-title").textContent = deviceName;
    updateOutputOwners();
    renderLogSignalOptions();
    renderRecentEvents();
    renderTimeline();
    renderLogs();
    renderHealth();
    renderOutputSettings(true);
    renderDashboardOutputs();
    refreshCurrentStateCards();
    renderCustomHistoryWidgets();
    renderChannelSettings();
    showToast("設定を保存しました");
  });
  document.querySelectorAll("[data-settings-target]").forEach((button) => {
    button.addEventListener("click", () => openSettingsSection(button.dataset.settingsTarget));
  });
  document.querySelector("#channel-settings").addEventListener("click", (event) => {
    const button = event.target.closest(".switch");
    if (!button) return;
    const enabled = !switchIsOn(button);
    setSwitchState(button, enabled);
    button.closest(".channel-row").classList.toggle("is-disabled", !enabled);
  });
  document.querySelector("#heartbeat-enabled").addEventListener("click", (event) => {
    setSwitchState(event.currentTarget, !switchIsOn(event.currentTarget));
    syncHeartbeatFields();
  });
  document.querySelector("#password-protection").addEventListener("click", (event) => {
    const enabled = !switchIsOn(event.currentTarget);
    setSwitchState(event.currentTarget, enabled);
    document.querySelector("#shared-password-field").hidden = !enabled;
  });
  document.querySelector("#setup-password-protection").addEventListener("click", (event) => {
    const enabled = !switchIsOn(event.currentTarget);
    setSwitchState(event.currentTarget, enabled);
    document.querySelector("#setup-password-field").hidden = !enabled;
  });
  document.querySelector("#setup-channel-list").addEventListener("click", (event) => {
    const button = event.target.closest(".switch");
    if (button) setSwitchState(button, !switchIsOn(button));
  });
  document.querySelector("#rule-enabled").addEventListener("click", (event) => {
    setSwitchState(event.currentTarget, !switchIsOn(event.currentTarget));
  });
  document.querySelector("#complete-setup").addEventListener("click", completeSetup);
  document.querySelector("#add-output-rule").addEventListener("click", () => openOutputRuleDialog());
  document.querySelector("#output-rule-list").addEventListener("click", (event) => {
    const switchButton = event.target.closest("[data-rule-enabled]");
    if (switchButton) {
      toggleOutputRule(Number(switchButton.dataset.ruleEnabled));
      return;
    }
    const button = event.target.closest("[data-edit-rule]");
    if (button) openOutputRuleDialog(Number(button.dataset.editRule));
  });
  document.querySelector("#rule-condition").addEventListener("change", updateRuleConditionFields);
  document.querySelector("#save-output-rule").addEventListener("click", saveOutputRule);
  document.querySelector("#delete-output-rule").addEventListener("click", async () => {
    const rule = fixtures.outputRules.find((item) => item.id === editingOutputRuleId);
    if (!rule) return;
    if (liveMode) {
      try {
        await liveJson(`/api/output-rules/${rule.id}`, { method: "DELETE" });
        await refreshLiveState();
        editingOutputRuleId = null;
        document.querySelector("#output-rule-dialog").close();
        showToast("条件ルールを削除しました");
      } catch (error) {
        showToast(error instanceof Error ? error.message : "条件ルールを削除できませんでした");
      }
      return;
    }
    fixtures.outputRules = fixtures.outputRules.filter((item) => item.id !== editingOutputRuleId);
    const output = fixtures.outputs.find((item) => item.id === rule.outputId);
    output.value = "OFF";
    editingOutputRuleId = null;
    updateOutputOwners();
    renderOutputSettings(true);
    renderLogSignalOptions();
    renderDashboardOutputs();
    renderHealth();
    renderDataViews();
    document.querySelector("#output-rule-dialog").close();
    showToast("条件ルールを削除しました");
  });
  document.querySelector("#widget-settings-list").addEventListener("click", (event) => {
    const addButton = event.target.closest("[data-add-widget-group]");
    if (addButton) {
      widgetEditor.editingRow = null;
      document.querySelector("#new-widget-title").value = "";
      openWidgetDialog(false, addButton.dataset.addWidgetGroup);
      return;
    }
    const button = event.target.closest(".switch");
    const row = event.target.closest(".widget-setting-row");
    const moveButton = event.target.closest(".move-up, .move-down");
    if (moveButton && row) {
      moveWidgetRow(row, moveButton.classList.contains("move-up") ? "up" : "down");
      return;
    }
    if (button) {
      const enabled = !switchIsOn(button);
      setSwitchState(button, enabled);
      const widget = dashboardWidgetById(row.dataset.widgetRow);
      if (widget) widget.classList.toggle("is-hidden-widget", !enabled);
      markWidgetSettingsDirty();
      showToast(enabled ? "ウィジェットを表示しました" : "ウィジェットを非表示にしました");
      return;
    }
    if (event.target.closest(".duplicate-widget") && row) {
      duplicateWidget(row);
      return;
    }
    if (event.target.closest(".delete-widget") && row) {
      deleteWidget(row);
      return;
    }
    if (event.target.closest(".edit-widget") && row) {
      widgetEditor.editingRow = row;
      document.querySelector("#new-widget-title").value = row.querySelector("strong").textContent;
      openWidgetDialog(true);
    }
  });
  document.querySelector("#widget-settings-list").addEventListener("change", (event) => {
    const sizeSelect = event.target.closest(".widget-size select");
    if (!sizeSelect) return;
    const row = sizeSelect.closest(".widget-setting-row");
    row.dataset.dashboardSize = sizeSelect.value;
    syncDashboardLayout();
    markWidgetSettingsDirty();
    showToast("表示幅を変更しました");
  });
  document.querySelector("#widget-settings-list").addEventListener("dragstart", (event) => {
    const row = event.target.closest(".widget-setting-row");
    if (!row || event.target.closest("button, select, input")) {
      event.preventDefault();
      return;
    }
    draggedWidgetRow = row;
    draggedWidgetOrigin = { parent: row.parentElement, next: row.nextElementSibling };
    dragWasDropped = false;
    row.classList.add("is-dragging");
    event.dataTransfer.effectAllowed = "move";
  });
  document.querySelector("#widget-settings-list").addEventListener("dragover", (event) => {
    const targetRow = event.target.closest(".widget-setting-row");
    if (
      !draggedWidgetRow
      || !targetRow
      || targetRow === draggedWidgetRow
      || targetRow.parentElement !== draggedWidgetRow.parentElement
    ) return;
    event.preventDefault();
    document.querySelectorAll(".widget-setting-row.is-drag-target").forEach((row) => {
      row.classList.remove("is-drag-target");
    });
    targetRow.classList.add("is-drag-target");
    const rect = targetRow.getBoundingClientRect();
    const insertAfter = event.clientY > rect.top + rect.height / 2;
    targetRow.parentElement.insertBefore(
      draggedWidgetRow,
      insertAfter ? targetRow.nextElementSibling : targetRow
    );
  });
  document.querySelector("#widget-settings-list").addEventListener("drop", (event) => {
    if (!draggedWidgetRow) return;
    event.preventDefault();
    dragWasDropped = true;
    syncDashboardLayout();
    markWidgetSettingsDirty();
    showToast("表示順を変更しました");
  });
  document.querySelector("#widget-settings-list").addEventListener("dragend", () => {
    document.querySelectorAll(".widget-setting-row.is-drag-target").forEach((row) => {
      row.classList.remove("is-drag-target");
    });
    if (draggedWidgetRow) {
      draggedWidgetRow.classList.remove("is-dragging");
      if (!dragWasDropped && draggedWidgetOrigin) draggedWidgetOrigin.parent.insertBefore(draggedWidgetRow, draggedWidgetOrigin.next);
    }
    draggedWidgetRow = null;
    draggedWidgetOrigin = null;
    dragWasDropped = false;
    updateMoveButtons();
  });
  document.querySelector("#widget-next").addEventListener("click", () => showWidgetStep(Math.min(3, currentWidgetStep + 1)));
  document.querySelector("#widget-back").addEventListener("click", () => showWidgetStep(Math.max(1, currentWidgetStep - 1)));
  document.querySelector("#new-widget-type").addEventListener("change", (event) => {
    document.querySelector("#widget-type-help").textContent = widgetTypeHelp[event.target.value];
    updateWidgetTypeFields();
  });
  document.querySelector("#new-widget-count-mode").addEventListener("change", updateWidgetTypeFields);
  document.querySelector("#new-widget-source").addEventListener("change", updateWidgetTypeFields);
  ["#new-widget-show-elapsed", "#new-widget-show-absolute", "#new-widget-show-event-elapsed"].forEach((selector) => {
    document.querySelector(selector).addEventListener("click", (event) => setSwitchState(event.currentTarget, !switchIsOn(event.currentTarget)));
  });
  document.querySelector("#confirm-add-widget").addEventListener("click", (event) => {
    const type = document.querySelector("#new-widget-type").value;
    if (["タイムライン", "イベント一覧"].includes(type) && !selectedWidgetChannels()) {
      event.preventDefault();
      showToast("表示する信号を1つ以上選択してください");
    }
  });
  document.querySelector("#widget-dialog").addEventListener("close", (event) => {
    if (event.target.returnValue !== "confirm") return;
    const title = document.querySelector("#new-widget-title").value || document.querySelector("#new-widget-type").value;
    const type = document.querySelector("#new-widget-type").value;
    const channelId = selectedWidgetChannels();
    const onLabel = document.querySelector("#new-widget-on-label").value || "ON";
    const offLabel = document.querySelector("#new-widget-off-label").value || "OFF";
    const showElapsed = boolControlValue(document.querySelector("#new-widget-show-elapsed"));
    const period = document.querySelector("#new-widget-period").value;
    const edge = document.querySelector("#new-widget-edge").value;
    const state = document.querySelector("#new-widget-state").value;
    const source = document.querySelector("#new-widget-source").value;
    const count = Number(document.querySelector("#new-widget-count").value);
    const value = document.querySelector("#new-widget-value").value;
    const countMode = document.querySelector("#new-widget-count-mode").value;
    const unit = document.querySelector("#new-widget-unit").value.trim() || "回";
    const timeUnit = document.querySelector("#new-widget-time-unit").value;
    const showAbsolute = boolControlValue(document.querySelector("#new-widget-show-absolute"));
    const showEventElapsed = boolControlValue(document.querySelector("#new-widget-show-event-elapsed"));
    markWidgetSettingsDirty();
    if (widgetEditor.editingRow) {
      widgetEditor.editingRow.querySelector("strong").textContent = title;
      widgetEditor.editingRow.dataset.widgetType = type;
      widgetEditor.editingRow.dataset.widgetChannel = channelId;
      widgetEditor.editingRow.dataset.onLabel = onLabel;
      widgetEditor.editingRow.dataset.offLabel = offLabel;
      widgetEditor.editingRow.dataset.showElapsed = String(showElapsed);
      widgetEditor.editingRow.dataset.widgetPeriod = period;
      widgetEditor.editingRow.dataset.widgetEdge = edge;
      widgetEditor.editingRow.dataset.widgetState = state;
      widgetEditor.editingRow.dataset.widgetSource = source;
      widgetEditor.editingRow.dataset.widgetCount = String(count);
      widgetEditor.editingRow.dataset.widgetValue = value;
      widgetEditor.editingRow.dataset.widgetCountMode = countMode;
      widgetEditor.editingRow.dataset.widgetUnit = unit;
      widgetEditor.editingRow.dataset.widgetTimeUnit = timeUnit;
      widgetEditor.editingRow.dataset.showAbsolute = String(showAbsolute);
      widgetEditor.editingRow.dataset.showEventElapsed = String(showEventElapsed);
      widgetEditor.editingRow.querySelector(".widget-size select").setAttribute("aria-label", `${title}の表示幅`);
      widgetEditor.editingRow.querySelector(".move-up").setAttribute("aria-label", `${title}を上へ移動`);
      widgetEditor.editingRow.querySelector(".move-down").setAttribute("aria-label", `${title}を下へ移動`);
      widgetEditor.editingRow.querySelector(".switch").setAttribute("aria-label", `${title}を表示`);
      widgetEditor.editingRow.querySelector("small").textContent = widgetSettingSummary(type, channelId, period, {
        showElapsed,
        count,
        countMode,
        source,
        edge,
        state
      });
      const widget = dashboardWidgetById(widgetEditor.editingRow.dataset.widgetRow);
      if (widget && type === "現在状態") {
        updateCurrentStateCard(widget, { title, channelId, onLabel, offLabel, showElapsed });
      } else {
        const widgetTitle = widget?.querySelector("[data-widget-title]");
        if (widgetTitle) widgetTitle.textContent = title;
        if (widget) {
          widget.dataset.widgetChannel = channelId;
          widget.dataset.widgetPeriod = period;
          widget.dataset.widgetEdge = edge;
          widget.dataset.widgetState = state;
          widget.dataset.widgetSource = source;
          widget.dataset.widgetCount = String(count);
          widget.dataset.widgetValue = value;
          widget.dataset.widgetCountMode = countMode;
          widget.dataset.widgetUnit = unit;
          widget.dataset.widgetTimeUnit = timeUnit;
          widget.dataset.showAbsolute = String(showAbsolute);
          widget.dataset.showEventElapsed = String(showEventElapsed);
        }
        if (widget?.matches("[data-custom-history-widget]")) {
          widget.dataset.widgetType = type;
          widget.dataset.widgetChannel = channelId;
          widget.dataset.widgetPeriod = period;
          renderHistoryWidgetCard(widget);
        }
        renderDashboardPeriod();
      }
      widgetEditor.editingRow.classList.add("is-highlighted");
      widgetEditor.editingRow = null;
      showToast("表示項目の設定を変更しました");
      return;
    }
    if (addingWidgetGroup === "realtime") {
      const realtimeList = document.querySelector('[data-widget-group="realtime"]');
      let number = realtimeList.children.length + 1;
      let id = `current-state-${number}`;
      while (document.querySelector(`[data-widget="${id}"]`)) {
        number += 1;
        id = `current-state-${number}`;
      }
      const config = { id, title, channelId, onLabel, offLabel, showElapsed, size: "medium" };
      realtimeList.insertAdjacentHTML("beforeend", currentStateSettingRowHtml(config));
      document.querySelector("#dashboard-realtime-grid").insertAdjacentHTML("beforeend", currentStateCardHtml(config));
      syncDashboardLayout();
      showToast("現在状態を追加しました");
      return;
    }
    const list = document.querySelector('[data-widget-group="history"]');
    const id = uniqueWidgetId("custom-widget");
      const config = { id, title, type, channelId, period, edge, state, source, count, value, countMode, unit, timeUnit, showAbsolute, showEventElapsed, size: type === "タイムライン" ? "large" : "small" };
    list.insertAdjacentHTML("beforeend", historyWidgetSettingRowHtml(config));
    document.querySelector("#dashboard-history-grid").insertAdjacentHTML("beforeend", historyWidgetCardHtml(config));
    syncDashboardLayout();
    bindTimelineEvents();
    showToast("表示項目を追加しました");
  });
  document.querySelector("#toggle-logging").addEventListener("click", () => {
    if (fixtures.loggerStatus === "critical") return;
    if (logging) {
      openConfirmation(
        "この記録装置の記録を停止しますか？",
        "停止中は入力信号を記録しません。\n動作確認信号・出力ルールを停止し、通常OUTをOFFにします。",
        "記録を停止",
        () => setLoggingState(false)
      );
    } else {
      setLoggingState(true);
    }
  });
  document.querySelector("#retry-config-sync").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (!liveMode) return;
    button.disabled = true;
    try {
      await liveJson("/api/config/reload", { method: "POST" });
      await refreshLiveState();
      showToast("loggerdへ現在の設定を再反映しました");
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  });
  document.querySelector("#output-tests").addEventListener("click", (event) => {
    const button = event.target.closest(".test-button");
    if (!button || logging || fixtures.loggerStatus === "critical") return;
    const outputId = Number(button.dataset.output);
    if (activeOutputTest?.outputId === outputId) {
      finishOutputTest();
    } else if (!activeOutputTest) {
      startOutputTest(outputId);
    }
  });
  document.querySelector("#confirm-dialog").addEventListener("close", (event) => {
    if (event.target.returnValue === "confirm" && pendingAction) pendingAction();
    pendingAction = null;
  });

  if (liveMode) renderPersistedWidgets(fixtures.widgets || []);
  initializeWidgetSwitchLabels();
  syncLiveControls();
  renderUnsynchronizedWarnings();
  updateOutputOwners();
  renderLogSignalOptions();
  renderOutputSettings();
  renderRecentEvents();
  syncDashboardLayout();
  document.querySelectorAll(".period-button").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.period === currentPeriod);
  });
  document.querySelector("#dashboard-custom-period").hidden = currentPeriod !== "custom";
  const savedCustomRange = fixtures.settings.dashboardCustomRange;
  const hasSavedCustomRange = (
    Number.isInteger(savedCustomRange?.from_ms)
    && Number.isInteger(savedCustomRange?.to_ms)
    && savedCustomRange.from_ms < savedCustomRange.to_ms
  );
  const customEndMs = hasSavedCustomRange
    ? savedCustomRange.to_ms
    : Math.floor(fixtures.nowMs / 60_000) * 60_000;
  const customStartMs = hasSavedCustomRange
    ? savedCustomRange.from_ms
    : Math.max(0, customEndMs - 4 * 60 * 60 * 1000);
  if (hasSavedCustomRange) {
    fixtures.dashboardPeriods.custom.startAt = new Date(savedCustomRange.from_ms).toISOString();
    fixtures.dashboardPeriods.custom.endAt = new Date(savedCustomRange.to_ms).toISOString();
    fixtures.dashboardPeriods.custom.durationMs = savedCustomRange.to_ms - savedCustomRange.from_ms;
  } else {
    // demoの固定時計や時刻変更後でも、初期期間を現在より未来にしない。
    fixtures.dashboardPeriods.custom.startAt = new Date(customStartMs).toISOString();
    fixtures.dashboardPeriods.custom.endAt = new Date(customEndMs).toISOString();
    fixtures.dashboardPeriods.custom.durationMs = customEndMs - customStartMs;
  }
  document.querySelector("#dashboard-custom-from").value = formatDateTimeInput(customStartMs);
  document.querySelector("#dashboard-custom-to").value = formatDateTimeInput(customEndMs);
  document.querySelector("#log-custom-from").value = formatDateTimeInput(customStartMs);
  document.querySelector("#log-custom-to").value = formatDateTimeInput(customEndMs);
  renderDashboardPeriod();
  renderDashboardOutputs();
  renderLogs();
  renderChannelSettings();
  renderHealth();
  renderOutputTests();
  renderSetupWizard();
  renderClock();
  window.setInterval(renderClock, 1000);
  window.setInterval(() => {
    refreshLiveState().catch((error) => {
      // 更新中はWebサービスが一時停止する。切断を更新失敗のtoastにしない。
      if (document.querySelector("#update-panel")?.dataset.updateProcessing !== "true") {
        showToast(error.message);
      }
    });
  }, 1000);
  }

  start().catch((error) => {
    const main = document.createElement("main");
    main.className = "main-content";
    const banner = document.createElement("div");
    banner.className = "banner banner--maintenance";
    const message = document.createElement("strong");
    message.textContent = `画面を読み込めませんでした：${error instanceof Error ? error.message : "予期しないエラー"}`;
    banner.append(message);
    main.append(banner);
    document.body.replaceChildren(main);
  });
})();
