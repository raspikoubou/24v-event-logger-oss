(() => {
  "use strict";

  // 信号周期をずらして、画面を開くと状態変化を順番に確認できるようにする。
  const channelSettings = [
    { id: "IN1", firstChangeMs: 2200, changeEveryMs: 4300 },
    { id: "IN2", firstChangeMs: 3900, changeEveryMs: 5400 },
    { id: "IN3", firstChangeMs: 5600, changeEveryMs: 6500 },
    { id: "IN4", firstChangeMs: 7400, changeEveryMs: 7700 },
    { id: "IN5", firstChangeMs: 9300, changeEveryMs: 9000 },
    { id: "IN6", firstChangeMs: 11300, changeEveryMs: 10400 }
  ];
  const historyLimit = 20;
  const simulation = {
    running: true,
    elapsedBeforeResumeMs: 0,
    resumedAtMs: performance.now(),
    resumedAtEpochMs: Date.now(),
    events: []
  };
  const channels = channelSettings.map((setting) => ({
    ...setting,
    state: "OFF",
    onCount: 0,
    onSinceMs: null,
    nextChangeAtMs: setting.firstChangeMs,
    card: null,
    stateValue: null,
    countValue: null,
    durationValue: null
  }));
  const channelGrid = document.getElementById("channel-grid");
  const eventHistory = document.getElementById("event-history");
  const simulationBadge = document.getElementById("simulation-badge");
  const simulationStatus = document.getElementById("simulation-status");
  const realtimeStatus = document.getElementById("realtime-status");
  const pauseToggle = document.getElementById("pause-toggle");
  const resetDemo = document.getElementById("reset-demo");

  function elapsedSimulationMs() {
    // 一時停止中に壁時計が進んでも時間を加算しないよう、実行区間だけを単調時計で測る。
    return simulation.elapsedBeforeResumeMs + (
      simulation.running ? performance.now() - simulation.resumedAtMs : 0
    );
  }

  function createChannelCard(channel) {
    const card = document.createElement("article");
    card.className = "channel-card is-off";
    card.dataset.channel = channel.id;
    card.setAttribute("role", "listitem");
    card.setAttribute("aria-label", `${channel.id}の状態`);

    const title = document.createElement("div");
    title.className = "channel-title";
    const channelName = document.createElement("span");
    channelName.className = "channel-name";
    channelName.textContent = channel.id;
    const inputType = document.createElement("span");
    inputType.textContent = "仮想入力";
    title.append(channelName, inputType);

    const stateValue = document.createElement("div");
    stateValue.className = "state-value";
    const indicator = document.createElement("span");
    indicator.className = "state-indicator";
    indicator.setAttribute("aria-hidden", "true");
    const stateLabel = document.createElement("span");
    stateLabel.textContent = channel.state;
    stateValue.append(indicator, stateLabel);

    const metrics = document.createElement("div");
    metrics.className = "card-metrics";
    const countMetric = document.createElement("div");
    const countLabel = document.createElement("span");
    countLabel.className = "metric-label";
    countLabel.textContent = "ONイベント回数";
    const countValue = document.createElement("strong");
    countValue.className = "metric-value";
    countValue.textContent = "0回";
    countMetric.append(countLabel, countValue);

    const durationMetric = document.createElement("div");
    const durationLabel = document.createElement("span");
    durationLabel.className = "metric-label";
    durationLabel.textContent = "ON継続時間";
    const durationValue = document.createElement("strong");
    durationValue.className = "metric-value";
    durationValue.textContent = "0秒";
    durationMetric.append(durationLabel, durationValue);
    metrics.append(countMetric, durationMetric);

    card.append(title, stateValue, metrics);
    channel.stateValue = stateLabel;
    channel.countValue = countValue;
    channel.durationValue = durationValue;
    channel.card = card;
    return card;
  }

  function renderChannels(elapsedMs) {
    channels.forEach((channel) => {
      const isOn = channel.state === "ON";
      channel.card.classList.toggle("is-on", isOn);
      channel.card.classList.toggle("is-off", !isOn);
      channel.stateValue.textContent = channel.state;
      channel.countValue.textContent = `${channel.onCount}回`;
      const onDurationMs = isOn && channel.onSinceMs !== null
        ? Math.max(0, elapsedMs - channel.onSinceMs)
        : 0;
      channel.durationValue.textContent = `${Math.floor(onDurationMs / 1000)}秒`;
    });
  }

  function renderHistory() {
    const rows = document.createDocumentFragment();
    if (simulation.events.length === 0) {
      const row = document.createElement("tr");
      row.className = "empty-row";
      const cell = document.createElement("td");
      cell.colSpan = 3;
      cell.textContent = "最初の信号変化を待機しています。";
      row.append(cell);
      rows.append(row);
    } else {
      simulation.events.forEach((event) => {
        const row = document.createElement("tr");
        const timeCell = document.createElement("td");
        timeCell.className = "event-time";
        const time = document.createElement("time");
        time.dateTime = event.at.toISOString();
        time.textContent = new Intl.DateTimeFormat("ja-JP", {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false
        }).format(event.at);
        timeCell.append(time);

        const channelCell = document.createElement("td");
        channelCell.className = "event-channel";
        channelCell.textContent = event.channelId;

        const stateCell = document.createElement("td");
        stateCell.className = `event-state is-${event.state.toLowerCase()}`;
        stateCell.textContent = event.state;
        row.append(timeCell, channelCell, stateCell);
        rows.append(row);
      });
    }
    eventHistory.replaceChildren(rows);
  }

  function renderControls() {
    const isRunning = simulation.running;
    simulationBadge.classList.toggle("is-stopped", !isRunning);
    const statusText = isRunning ? "自動実行中" : "一時停止中";
    simulationStatus.textContent = statusText;
    realtimeStatus.textContent = isRunning ? "自動切替中" : "一時停止中";
    realtimeStatus.classList.toggle("is-stopped", !isRunning);
    pauseToggle.textContent = isRunning ? "一時停止" : "再開";
    pauseToggle.setAttribute("aria-pressed", String(!isRunning));
  }

  function render(elapsedMs = elapsedSimulationMs(), historyChanged = false) {
    renderChannels(elapsedMs);
    renderControls();
    if (historyChanged) renderHistory();
  }

  function recordChange(change) {
    // 各実行区間の壁時計基準と予定シミュレーション時刻から発生日時を復元する。
    // 一時停止で空いた時間は、再開時に新しい基準を置くため信号の経過時間へ混ざらない。
    simulation.events.unshift({
      at: new Date(
        simulation.resumedAtEpochMs + change.atMs - simulation.elapsedBeforeResumeMs
      ),
      channelId: change.channel.id,
      state: change.state
    });
    if (simulation.events.length > historyLimit) {
      simulation.events.length = historyLimit;
    }
  }

  function advanceSimulation(elapsedMs) {
    const changes = [];
    channels.forEach((channel) => {
      while (elapsedMs >= channel.nextChangeAtMs) {
        channel.state = channel.state === "OFF" ? "ON" : "OFF";
        if (channel.state === "ON") {
          channel.onCount += 1;
          channel.onSinceMs = channel.nextChangeAtMs;
        } else {
          channel.onSinceMs = null;
        }
        changes.push({ channel, state: channel.state, atMs: channel.nextChangeAtMs });
        channel.nextChangeAtMs += channel.changeEveryMs;
      }
    });
    changes.sort((left, right) => left.atMs - right.atMs);
    changes.forEach((change) => recordChange(change));
    return changes.length > 0;
  }

  function tick() {
    if (!simulation.running) return;
    const elapsedMs = elapsedSimulationMs();
    const historyChanged = advanceSimulation(elapsedMs);
    render(elapsedMs, historyChanged);
  }

  channels.forEach((channel) => channelGrid.append(createChannelCard(channel)));
  render();

  pauseToggle.addEventListener("click", () => {
    if (simulation.running) {
      const elapsedMs = elapsedSimulationMs();
      const historyChanged = advanceSimulation(elapsedMs);
      simulation.elapsedBeforeResumeMs = elapsedMs;
      simulation.running = false;
      render(elapsedMs, historyChanged);
      return;
    }
    simulation.resumedAtMs = performance.now();
    simulation.resumedAtEpochMs = Date.now();
    simulation.running = true;
    render();
  });

  resetDemo.addEventListener("click", () => {
    channels.forEach((channel) => {
      channel.onCount = 0;
    });
    simulation.events = [];
    render(elapsedSimulationMs(), true);
  });

  window.setInterval(tick, 250);
})();
