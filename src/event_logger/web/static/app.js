(() => {
  "use strict";

  // Sitesは固定fixtureのまま確認でき、Piのテンプレートだけが実機APIを有効化する。
  const liveMode = Boolean(window.EVENT_LOGGER_LIVE);

  const {
    createRefreshCoordinator,
    shouldRefreshDashboard,
    shouldDeferTimelineRender,
    shouldDeferTimelineWidgetRender,
    shouldRefreshLogPage
  } = window.EventLoggerRefreshPolicy;

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
    const status = await liveJson("/api/status");
    const storageWarning = storageWarningFor(
      status.storage,
      Boolean(status.storage_error)
    );
    const selectedPeriod = period || status.dashboard_period || "8h";
    const selectedCustomRange = customRange || status.dashboard_custom_range;
    const [widgets, rules, setup, logs, dashboard] = await Promise.all([
      liveJson("/api/widgets"),
      liveJson("/api/output-rules"),
      liveJson("/api/setup"),
      includeEvents ? liveJson("/api/logs?limit=500") : Promise.resolve(null),
      includeDashboard
        ? liveJson(dashboardApiUrl(selectedPeriod, selectedCustomRange))
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
    const knownLoggerState = ["logging", "maintenance", "critical"].includes(status.logger_status);
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
            ? `${Number(status.daemon.write_latency.delayed_count_5m || 0)}回 · 最大${formatMilliseconds(status.daemon.write_latency.max_delay_ms_5m || 0)}ms · 最長滞留${formatMilliseconds(status.daemon.write_latency.oldest_pending_ms || 0)}ms · 未保存${Number(status.daemon.pending_event_count || 0)}件`
            : "--",
          state: status.daemon?.write_latency?.warning ? "warning" : "ok"
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
    storageWarningMessage,
    warningVisibilityFor
  } = window.EventLoggerStoragePolicy;

  async function start() {
    const fixtures = await loadLiveFixtures(null, liveMode);
    runApp(fixtures);
  }

  function runApp(fixtures) {
  let currentPeriod = fixtures.settings.dashboardPeriod || "8h";
  let dashboardCustomRangeDraft = null;
  let logging = fixtures.loggerStatus === "logging";
  let pendingAction = null;
  let editingWidgetRow = null;
  let addingWidgetGroup = "realtime";
  let widgetSettingsDirty = false;
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
  let logPage = 1;
  let logPageEvents = [];
  let logTotalCount = 0;
  let logHasNext = false;
  let logPageLoaded = false;
  let logPageLoading = false;
  let logRequestId = 0;
  let logSearchRange = null;
  let nextLogRefreshAtMs = 0;
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

  function demoNowMs() {
    return demoClockBaseMs + performance.now() - demoClockStartedAtMs;
  }

  const refreshLiveState = createRefreshCoordinator(refreshLiveStateOnce);

  async function refreshLiveStateOnce(forceDashboard = false) {
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
      const bounds = next.dashboardPeriods[currentPeriod];
      if (bounds) fixtures.dashboardPeriods[currentPeriod] = bounds;
    }
    if (next.dashboardWarning) nextDashboardRefreshAtMs = Date.now() + 30_000;
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
      && !widgetSettingsDirty
      && !widgetDialogOpen
      && JSON.stringify(fixtures.widgets || []) !== JSON.stringify(nextWidgets)
    ) {
      fixtures.widgets = nextWidgets;
      renderPersistedWidgets(nextWidgets);
    }
    renderDataViews();
    if (
      shouldRefreshLogPage({
        activeView,
        liveMode,
        logPageLoading,
        periodKey: document.querySelector("#log-period").value,
        rangeEndMs: periodBounds(document.querySelector("#log-period").value).endMs,
        nextRefreshAtMs: nextLogRefreshAtMs,
        nowMs: Date.now()
      })
    ) {
      if (logPage === 1 && document.querySelector("#log-period").value !== "custom") {
        logSearchRange = null;
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
  }

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

  function renderUnsynchronizedWarnings() {
    const count = fixtures.unsynchronizedCount;
    const latencyWarning = Boolean(fixtures.writeLatency?.warning);
    const configurationWarning = Boolean(fixtures.configurationSync?.outOfSync);
    const storageWarning = storageWarningFor(fixtures.storage, fixtures.storageError);
    const warningVisibility = warningVisibilityFor({
      count,
      latencyWarning,
      configurationWarning,
      storageVisible: storageWarning.visible
    });
    const { syncVisible, visible } = warningVisibility;
    const banner = document.querySelector(".banner--warning");
    const storageBanner = document.querySelector("#storage-warning-banner");
    const detail = document.querySelector("#unsynchronized-warning");
    const latencyDetail = document.querySelector("#write-latency-warning");
    const configurationDetail = document.querySelector("#configuration-sync-warning");
    const storageDetail = document.querySelector("#storage-warning-detail");
    const retryConfiguration = document.querySelector("#retry-config-sync");
    const pill = document.querySelector("#diagnostic-warning-count");
    const warningCount = Number(count > 0) + Number(latencyWarning)
      + Number(configurationWarning) + Number(storageWarning.visible);
    banner.hidden = !syncVisible;
    storageBanner.hidden = !storageWarning.visible;
    storageBanner.classList.toggle("banner--critical", storageWarning.severity === "critical");
    storageBanner.querySelector(".banner-icon").textContent = storageWarning.severity === "critical" ? "×" : "!";
    storageBanner.querySelector("strong").textContent = storageWarning.title;
    storageBanner.querySelector("#storage-warning-message").textContent = storageWarning.visible
      ? storageWarningMessage(storageWarning)
      : "";
    detail.hidden = count === 0;
    latencyDetail.hidden = !latencyWarning;
    configurationDetail.hidden = !configurationWarning;
    storageDetail.hidden = !storageWarning.visible;
    storageDetail.classList.toggle("diagnostic-warning--critical", storageWarning.severity === "critical");
    storageDetail.querySelector("strong").textContent = storageWarning.title;
    storageDetail.querySelector("span").textContent = storageWarning.visible
      ? storageWarningMessage(storageWarning)
      : "";
    retryConfiguration.hidden = !configurationWarning || !liveMode;
    pill.hidden = !visible;
    pill.classList.toggle("critical", storageWarning.severity === "critical");
    pill.classList.toggle("warning", storageWarning.severity !== "critical");
    pill.textContent = `確認事項 ${warningCount}件`;
    if (!syncVisible) return;
    const title = configurationWarning
      ? "loggerdの設定反映を確認してください"
      : latencyWarning ? "ログ保存に遅延があります" : `同期前の記録が${count}件あります`;
    banner.querySelector("strong").textContent = title;
    if (count > 0) {
      const clockTitle = `同期前の記録が${count}件あります`;
      detail.querySelector("strong").textContent = clockTitle;
      detail.querySelector("span").textContent = `現在の時刻は同期済みのため、時刻設定は不要です。同期前の${count}件だけ、発生時刻が正確でない可能性があります。必要な記録の場合は、ログ画面で時刻が「要確認」の行を確認してください。`;
    }
    if (latencyWarning) {
      latencyDetail.querySelector("span").textContent = `直近5分に500ms以上の保存が${fixtures.writeLatency.delayed_count_5m}回（最大${formatMilliseconds(fixtures.writeLatency.max_delay_ms_5m)}ms）、未保存の最長滞留${formatMilliseconds(fixtures.writeLatency.oldest_pending_ms)}ms。入力頻度とSDカードの状態を確認してください。`;
    }
    if (configurationWarning) {
      configurationDetail.querySelector("span").textContent = fixtures.configurationSync.message;
    }
  }

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

  function signalDisplayName(signalId) {
    const match = /^(IN|OUT)(\d+)$/.exec(signalId);
    if (!match) return signalId;
    const collection = match[1] === "IN" ? fixtures.channels : fixtures.outputs;
    const item = collection.find((entry) => entry.id === Number(match[2]));
    return item?.name || signalId;
  }

  const { signalIds, channelLabel, escapeHtml } = window.EventLoggerViewFormat;

  function switchIsOn(button) {
    return button.classList.contains("is-on");
  }

  function setSwitchState(button, enabled) {
    button.classList.toggle("is-on", enabled);
    button.setAttribute("aria-checked", String(enabled));
  }

  function boolControlValue(button) {
    return switchIsOn(button);
  }

  function initializeWidgetSwitchLabels() {
    document.querySelectorAll("#widget-settings-list .switch").forEach((button) => {
      button.dataset.onLabel ||= "表示中";
      button.dataset.offLabel ||= "非表示";
    });
  }

  function showToast(message) {
    const toast = document.querySelector("#toast");
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2400);
  }

  function switchView(viewName) {
    activeView = viewName;
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

  function timelinePeriodBounds() {
    const card = document.querySelector('[data-widget="timeline"]');
    return periodBounds(periodKeyForWidget(card.dataset.widgetPeriod || "follow"));
  }

  function formatTimelineTime(timestampMs) {
    return formatLocalDateTime(timestampMs, true);
  }

  // 表示だけを丸め、記録時刻や集計値の精度は変更しない。
  function formatMilliseconds(value) {
    return Number(value).toFixed(1);
  }

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

  function currentStateValues(channelId, onLabel, offLabel) {
    const channel = fixtures.channels.find((item) => `IN${item.id}` === channelId);
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

  function updateCurrentStateCard(card, config) {
    const values = currentStateValues(config.channelId, config.onLabel, config.offLabel);
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

  function currentStateCardHtml(config) {
    const values = currentStateValues(config.channelId, config.onLabel, config.offLabel);
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

  function sizeOptionsHtml(selectedSize) {
    const labels = { small: "小", medium: "中", large: "大" };
    return Object.entries(labels).map(([value, label]) => (
      `<option value="${value}"${value === selectedSize ? " selected" : ""}>${label}</option>`
    )).join("");
  }

  function refreshCurrentStateCards() {
    document.querySelectorAll("#dashboard-realtime-grid [data-widget-type='現在状態']").forEach((card) => {
      updateCurrentStateCard(card, {
        title: card.querySelector("[data-widget-title]").textContent,
        channelId: card.dataset.widgetChannel,
        onLabel: card.dataset.onLabel,
        offLabel: card.dataset.offLabel,
        showElapsed: card.dataset.showElapsed === "true"
      });
    });
  }

  function periodKeyForWidget(periodSetting) {
    return periodSetting === "follow" ? currentPeriod : periodSetting;
  }

  function periodBounds(periodKey, durationOverrideMs = 0) {
    const period = fixtures.dashboardPeriods[periodKey];
    const endMs = period.endAt ? Date.parse(period.endAt) : demoNowMs();
    const durationMs = Number.isFinite(durationOverrideMs) && durationOverrideMs > 0
      ? durationOverrideMs
      : period.durationMs;
    const startMs = period.startAt ? Date.parse(period.startAt) : endMs - durationMs;
    return { startMs, endMs };
  }

  function eventTimeMs(event) {
    return event.timestamp ? Date.parse(event.timestamp) : Date.parse(`2026-09-12T${event.time}+09:00`);
  }

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

  function channelEventsInPeriod(channelId, periodKey) {
    const { startMs, endMs } = periodBounds(periodKey);
    return fixtures.events.filter((event) => {
      const timeMs = eventTimeMs(event);
      return event.signal === channelId && event.kind === "edge" && timeMs >= startMs && timeMs <= endMs;
    });
  }

  function stateDurationInPeriod(channelId, periodKey, targetState = "ON", durationOverrideMs = 0) {
    return timelineIntervals(channelId, periodKey, durationOverrideMs)
      .filter((interval) => interval.state === targetState)
      .reduce((total, interval) => total + interval.endMs - interval.startMs, 0);
  }

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

  function timelineMarkClass(classPrefix, unknown, widthPercent) {
    // 長い期間では短い区間の幅が0px相当になり、hover/clickできなくなる。
    // 1%未満は時間幅を誇張せず、詳細を開ける固定マーカーで示す。
    const isNarrow = widthPercent < 1;
    if (unknown) {
      return `${classPrefix}-${isNarrow ? "unknown-marker" : "unknown"}`;
    }
    return `${classPrefix}-${isNarrow ? "marker" : "segment"}`;
  }

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

  function renderCustomHistoryWidgets() {
    document.querySelectorAll("[data-custom-history-widget]").forEach(renderHistoryWidgetCard);
  }

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

  function widgetApiPayloadFromRow(row, sortOrder) {
    // DOMの読取りはUI側、形式変換は専用モジュールに分離する。
    return widgetPayloadFromConfig(
      widgetConfigFromRow(row),
      sortOrder,
      switchIsOn(row.querySelector(".switch")),
      row.dataset.widgetSource
    );
  }

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

  function markWidgetSettingsDirty() {
    if (liveMode) widgetSettingsDirty = true;
  }

  async function saveWidgetSettings() {
    if (!liveMode || !widgetSettingsDirty) return;
    // 途中の通信失敗で一部だけ保存された場合も、再試行時はDBの最新状態を
    // 基準に差分を作る。古い一覧のままPOSTすると重複IDで復旧できなくなる。
    const current = await liveJson("/api/widgets");
    fixtures.widgets = current.items || [];
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
    const response = await liveJson("/api/widgets");
    fixtures.widgets = response.items || [];
    widgetSettingsDirty = false;
    renderPersistedWidgets(fixtures.widgets);
  }

  // rolling期間の境界は更新されるため、描画済み区間の詳細は絶対時刻を優先する。
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

  function kindLabel(event) {
    if (event.kind === "output") return "出力の記録";
    if (event.kind === "snapshot") return "記録開始時の状態";
    return "入力の変化";
  }

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

  function renderLogs() {
    const pageSize = Number(document.querySelector("#log-page-size").value);
    const events = liveMode ? logPageEvents : filteredEvents();
    const totalCount = liveMode ? logTotalCount : events.length;
    const pageCount = Math.max(1, Math.ceil(totalCount / pageSize));
    logPage = Math.min(logPage, pageCount);
    const pageEvents = liveMode
      ? events
      : events.slice((logPage - 1) * pageSize, logPage * pageSize);
    const tbody = document.querySelector("#log-table-body");
    document.querySelector("#log-count").textContent = liveMode && !logPageLoaded
      ? "--件"
      : `${totalCount}件`;
    document.querySelector("#log-page-label").textContent = `${logPage} / ${pageCount}ページ`;
    document.querySelector("#log-prev").disabled = logPageLoading || logPage === 1;
    document.querySelector("#log-next").disabled = logPageLoading || (
      liveMode ? !logHasNext : logPage === pageCount
    );
    if (liveMode && !logPageLoaded) {
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

  function logApiQuery(includePaging) {
    const periodKey = document.querySelector("#log-period").value;
    return buildLogApiQuery({
      periodKey,
      customBounds: periodKey === "custom" ? periodBounds(periodKey) : null,
      logSearchRange,
      selectedSignal: document.querySelector("#log-signal").value,
      widgetLogSignals,
      selectedValue: document.querySelector("#log-value").value,
      selectedKind: document.querySelector("#log-kind").value,
      selectedSource: document.querySelector("#log-source").value,
      includePaging,
      pageSize: includePaging ? Number(document.querySelector("#log-page-size").value) : null,
      logPage
    });
  }

  async function loadLiveLogPage() {
    const requestId = ++logRequestId;
    logPageLoading = true;
    renderLogs();
    try {
      const result = await liveJson(`/api/logs?${logApiQuery(true)}`);
      if (requestId !== logRequestId) return;
      logSearchRange = {
        fromMs: Number(result.from_ms),
        toMs: Number(result.to_ms)
      };
      logTotalCount = Number(result.total_count || 0);
      const pageSize = Number(document.querySelector("#log-page-size").value);
      const pageCount = Math.max(1, Math.ceil(logTotalCount / pageSize));
      if (logPage > pageCount) {
        logPage = pageCount;
        await loadLiveLogPage();
        return;
      }
      logPageEvents = (result.items || []).map((event, index) => ({
        id: Number(event.id?.split(":").pop()) || index + 1,
        timestamp: event.timestamp,
        signal: event.signal,
        value: event.value,
        kind: event.kind,
        source: event.source || "",
        synced: Boolean(event.clock_synced)
      }));
      logHasNext = Boolean(result.has_next);
      logPageLoaded = true;
    } finally {
      if (requestId === logRequestId) {
        logPageLoading = false;
        nextLogRefreshAtMs = Date.now() + 5_000;
      }
      renderLogs();
    }
  }

  function reloadLogQuery() {
    logPage = 1;
    // フィルター変更後は新たな時間範囲を検索する。ページ送りの間だけ範囲を固定する。
    logSearchRange = null;
    if (liveMode) {
      logPageLoaded = false;
      logPageEvents = [];
      logTotalCount = 0;
      logHasNext = false;
      loadLiveLogPage().catch((error) => showToast(error.message));
    } else {
      renderLogs();
    }
  }

  function renderDataViews() {
    renderLogSignalOptions();
    renderRecentEvents();
    renderTimeline();
    renderLogs();
    renderDashboardPeriod();
    renderCustomHistoryWidgets();
  }

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

  async function completeSetup() {
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

  async function finishOutputTest(showMessage = true) {
    if (!activeOutputTest) return;
    if (liveMode) {
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

  async function startOutputTest(outputId) {
    if (liveMode) {
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

  function durationLabel(rule) {
    const units = { seconds: "秒", minutes: "分", hours: "時間" };
    return `${rule.duration}${units[rule.durationUnit]}`;
  }

  function ruleDescription(rule) {
    const name = signalDisplayName(rule.channelId);
    const signalLabel = name === rule.channelId ? rule.channelId : `${rule.channelId} ${name}`;
    if (rule.condition === "event_absence") {
      const edge = rule.edge === "ON" ? "OFF→ON" : "ON→OFF";
      return `${signalLabel}の${edge}が${durationLabel(rule)}発生しない`;
    }
    return `${signalLabel}が${rule.value}のまま${durationLabel(rule)}継続`;
  }

  function syncHeartbeatFields() {
    const enabled = switchIsOn(document.querySelector("#heartbeat-enabled"));
    document.querySelector("#heartbeat-output").disabled = !enabled;
    document.querySelector("#heartbeat-interval").disabled = !enabled;
  }

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

  function populateInputSelect(select, selectedId) {
    select.innerHTML = fixtures.channels.map((channel) => (
      `<option value="IN${channel.id}">${escapeHtml(channelLabel(channel))}</option>`
    )).join("");
    select.value = selectedId || "IN1";
  }

  function updateRuleConditionFields() {
    const absence = document.querySelector("#rule-condition").value === "event_absence";
    document.querySelector("#rule-value-field").hidden = absence;
    document.querySelector("#rule-edge-field").hidden = !absence;
  }

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

  function outputConflict(outputId, ignoredRuleId = null) {
    const heartbeat = fixtures.settings.heartbeat;
    if (heartbeat.enabled && heartbeat.outputId === outputId) return "Heartbeat";
    const rule = fixtures.outputRules.find((item) => item.outputId === outputId && item.id !== ignoredRuleId);
    return rule?.name || "";
  }

  function validRuleDuration(value) {
    const duration = Number(value);
    return Number.isInteger(duration) && duration >= 1 && duration <= 86400;
  }

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

  async function setLoggingState(nextLogging) {
    if (liveMode) {
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

  function openConfirmation(title, message, confirmLabel, action) {
    const dialog = document.querySelector("#confirm-dialog");
    document.querySelector("#dialog-title").textContent = title;
    document.querySelector("#dialog-message").textContent = message;
    document.querySelector("#dialog-confirm").textContent = confirmLabel;
    pendingAction = action;
    dialog.showModal();
  }

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

  function setWidgetTypeOptions(group, selectedType) {
    const types = group === "realtime"
      ? ["現在状態"]
      : ["イベント回数", "ON/OFF時間", "最終イベント", "経過時間", "タイムライン", "イベント一覧"];
    const typeSelect = document.querySelector("#new-widget-type");
    typeSelect.innerHTML = types.map((type) => `<option>${type}</option>`).join("");
    typeSelect.value = types.includes(selectedType) ? selectedType : types[0];
    document.querySelector("#widget-type-help").textContent = widgetTypeHelp[typeSelect.value];
  }

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

  function setWidgetChannels(value) {
    const ids = signalIds(value);
    document.querySelector("#new-widget-channel").value = ids[0] || "IN1";
    document.querySelectorAll("#widget-channel-checks input").forEach((input) => {
      input.checked = ids.includes(input.value);
    });
  }

  function selectedWidgetChannels() {
    const type = document.querySelector("#new-widget-type").value;
    if (!["タイムライン", "イベント一覧"].includes(type)) return document.querySelector("#new-widget-channel").value;
    const checked = [...document.querySelectorAll("#widget-channel-checks input:checked")].map((input) => input.value);
    return checked.join(",");
  }

  function openWidgetDialog(isEditing, widgetGroup = "realtime") {
    const typeSelect = document.querySelector("#new-widget-type");
    typeSelect.disabled = isEditing;
    renderWidgetChannelOptions();
    if (isEditing && editingWidgetRow) {
      addingWidgetGroup = editingWidgetRow.closest(".widget-group-list").dataset.widgetGroup;
      setWidgetTypeOptions(addingWidgetGroup, widgetTypeForRow(editingWidgetRow));
      setWidgetChannels(widgetChannelForRow(editingWidgetRow));
      document.querySelector("#new-widget-on-label").value = editingWidgetRow.dataset.onLabel || "ON";
      document.querySelector("#new-widget-off-label").value = editingWidgetRow.dataset.offLabel || "OFF";
      setSwitchState(document.querySelector("#new-widget-show-elapsed"), editingWidgetRow.dataset.showElapsed !== "false");
      document.querySelector("#new-widget-period").value = editingWidgetRow.dataset.widgetPeriod || "follow";
      document.querySelector("#new-widget-edge").value = editingWidgetRow.dataset.widgetEdge || "ON";
      document.querySelector("#new-widget-state").value = editingWidgetRow.dataset.widgetState || "ON";
      document.querySelector("#new-widget-source").value = editingWidgetRow.dataset.widgetSource || "state";
      document.querySelector("#new-widget-count").value = editingWidgetRow.dataset.widgetCount || "10";
      document.querySelector("#new-widget-value").value = editingWidgetRow.dataset.widgetValue || "all";
      document.querySelector("#new-widget-count-mode").value = editingWidgetRow.dataset.widgetCountMode || "period";
      document.querySelector("#new-widget-unit").value = editingWidgetRow.dataset.widgetUnit || "回";
      document.querySelector("#new-widget-time-unit").value = editingWidgetRow.dataset.widgetTimeUnit || "human";
      setSwitchState(document.querySelector("#new-widget-show-absolute"), editingWidgetRow.dataset.showAbsolute !== "false");
      setSwitchState(document.querySelector("#new-widget-show-event-elapsed"), editingWidgetRow.dataset.showEventElapsed === "true");
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

  function widgetApiConfigForRow(row) {
    try {
      const config = JSON.parse(row.dataset.apiConfig || "{}");
      return config && typeof config === "object" && !Array.isArray(config) ? config : {};
    } catch {
      return {};
    }
  }

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

  function dashboardWidgetById(widgetId) {
    return [...document.querySelectorAll("#dashboard-realtime-grid [data-widget], #dashboard-history-grid [data-widget]")]
      .find((widget) => widget.dataset.widget === widgetId) || null;
  }

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

  function uniqueWidgetId(prefix = "widget") {
    let number = 1;
    while (document.querySelector(`[data-widget="${prefix}-${number}"], [data-widget-row="${prefix}-${number}"]`)) number += 1;
    return `${prefix}-${number}`;
  }

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
    logSearchRange = null;
    logPage = 1;
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
        logSearchRange = null;
      }
      if (select.id === "log-source" && select.value !== "all") {
        document.querySelector("#log-kind").value = "output";
      }
      reloadLogQuery();
    });
  });
  document.querySelector("#apply-log-custom-period").addEventListener("click", () => {
    if (!applyCustomPeriod(document.querySelector("#log-custom-from"), document.querySelector("#log-custom-to"))) return;
    logSearchRange = null;
    reloadLogQuery();
    showToast("任意期間を適用しました");
  });
  document.querySelector("#log-page-size").addEventListener("change", () => {
    reloadLogQuery();
  });
  document.querySelector("#log-prev").addEventListener("click", () => {
    logPage = Math.max(1, logPage - 1);
    if (liveMode) loadLiveLogPage().catch((error) => showToast(error.message));
    else renderLogs();
  });
  document.querySelector("#log-next").addEventListener("click", () => {
    logPage += 1;
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
        await saveWidgetSettings();
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
      editingWidgetRow = null;
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
      editingWidgetRow = row;
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
    if (editingWidgetRow) {
      editingWidgetRow.querySelector("strong").textContent = title;
      editingWidgetRow.dataset.widgetType = type;
      editingWidgetRow.dataset.widgetChannel = channelId;
      editingWidgetRow.dataset.onLabel = onLabel;
      editingWidgetRow.dataset.offLabel = offLabel;
      editingWidgetRow.dataset.showElapsed = String(showElapsed);
      editingWidgetRow.dataset.widgetPeriod = period;
      editingWidgetRow.dataset.widgetEdge = edge;
      editingWidgetRow.dataset.widgetState = state;
      editingWidgetRow.dataset.widgetSource = source;
      editingWidgetRow.dataset.widgetCount = String(count);
      editingWidgetRow.dataset.widgetValue = value;
      editingWidgetRow.dataset.widgetCountMode = countMode;
      editingWidgetRow.dataset.widgetUnit = unit;
      editingWidgetRow.dataset.widgetTimeUnit = timeUnit;
      editingWidgetRow.dataset.showAbsolute = String(showAbsolute);
      editingWidgetRow.dataset.showEventElapsed = String(showEventElapsed);
      editingWidgetRow.querySelector(".widget-size select").setAttribute("aria-label", `${title}の表示幅`);
      editingWidgetRow.querySelector(".move-up").setAttribute("aria-label", `${title}を上へ移動`);
      editingWidgetRow.querySelector(".move-down").setAttribute("aria-label", `${title}を下へ移動`);
      editingWidgetRow.querySelector(".switch").setAttribute("aria-label", `${title}を表示`);
      editingWidgetRow.querySelector("small").textContent = widgetSettingSummary(type, channelId, period, {
        showElapsed,
        count,
        countMode,
        source,
        edge,
        state
      });
      const widget = dashboardWidgetById(editingWidgetRow.dataset.widgetRow);
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
      editingWidgetRow.classList.add("is-highlighted");
      editingWidgetRow = null;
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
        "設備の運転は停止しません。\n\n停止中は入力信号を記録しません。\n動作確認信号・出力ルールを停止し、通常OUTをOFFにします。",
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
    refreshLiveState().catch((error) => showToast(error.message));
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
