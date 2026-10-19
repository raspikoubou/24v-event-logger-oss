(() => {
  "use strict";

  // 各信号の周期をずらし、ページを開くと変化を順に見られるようにする。
  const channelSettings = [
    { id: "IN1", firstChangeMs: 2200, changeEveryMs: 4300 },
    { id: "IN2", firstChangeMs: 3900, changeEveryMs: 5400 },
    { id: "IN3", firstChangeMs: 5600, changeEveryMs: 6500 },
    { id: "IN4", firstChangeMs: 7400, changeEveryMs: 7700 },
    { id: "IN5", firstChangeMs: 9300, changeEveryMs: 9000 },
    { id: "IN6", firstChangeMs: 11300, changeEveryMs: 10400 }
  ];
  const historyLimit = 20;
  const timelineLimit = 600;
  const simulation = {
    running: true,
    elapsedBeforeResumeMs: 0,
    resumedAtMs: performance.now(),
    resumedAtEpochMs: Date.now(),
    events: [],
    timelineEvents: []
  };
  const channels = channelSettings.map(function (setting) {
    return {
      id: setting.id,
      changeEveryMs: setting.changeEveryMs,
      state: "OFF",
      onCount: 0,
      onSinceMs: null,
      nextChangeAtMs: setting.firstChangeMs,
      timelineStartAtMs: 0,
      timelineStartState: "OFF",
      card: null,
      stateValue: null,
      countValue: null,
      durationValue: null,
      diagnosticValue: null
    };
  });
  const channelGrid = document.getElementById("channel-grid");
  const eventList = document.getElementById("demo-event-list");
  const eventTable = document.getElementById("log-table-body");
  const loggingBadge = document.getElementById("logging-badge");
  const loggingStatus = document.getElementById("logging-status");
  const realtimeStatus = document.getElementById("realtime-status");
  const simulationToggle = document.getElementById("simulation-toggle");
  const resetDemo = document.getElementById("reset-demo");

  function elapsedSimulationMs() {
    // 一時停止で壁時計だけが進んでも、継続時間や周期に混ぜない。
    return simulation.elapsedBeforeResumeMs + (
      simulation.running ? performance.now() - simulation.resumedAtMs : 0
    );
  }

  function formatClock(date) {
    return new Intl.DateTimeFormat("ja-JP", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }).format(date);
  }

  function formatLongTime(date) {
    const parts = new Intl.DateTimeFormat("ja-JP", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }).formatToParts(date);
    const get = function (type) {
      const part = parts.find(function (item) { return item.type === type; });
      return part ? part.value : "";
    };
    return get("year") + "/" + get("month") + "/" + get("day") + " " +
      get("hour") + ":" + get("minute") + ":" + get("second");
  }

  function formatDuration(durationMs) {
    const totalSeconds = Math.max(0, Math.floor(durationMs / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    if (hours > 0) return hours + "時間" + minutes + "分継続";
    if (minutes > 0) return minutes + "分" + seconds + "秒継続";
    return seconds + "秒継続";
  }

  function createChannelCard(channel) {
    const card = document.createElement("article");
    card.className = "overview-card is-normal-state";
    card.dataset.size = "small";
    card.dataset.channel = channel.id;
    card.setAttribute("role", "listitem");
    card.setAttribute("aria-label", channel.id + "の現在状態");

    const title = document.createElement("div");
    title.className = "overview-label";
    title.textContent = "現在状態";

    const state = document.createElement("strong");
    state.textContent = channel.state;

    const duration = document.createElement("p");
    duration.textContent = "0秒継続";

    const meta = document.createElement("small");
    meta.className = "demo-channel-meta";
    meta.textContent = channel.id + "　ONイベント 0回";

    card.append(title, state, duration, meta);
    channel.stateValue = state;
    channel.durationValue = duration;
    channel.countValue = meta;
    channel.card = card;
    return card;
  }

  function renderChannels(elapsedMs) {
    channels.forEach(function (channel) {
      const isOn = channel.state === "ON";
      channel.card.classList.toggle("is-active-state", isOn);
      channel.card.classList.toggle("is-normal-state", !isOn);
      channel.stateValue.textContent = channel.state;
      channel.countValue.textContent = channel.id + "　ONイベント " + channel.onCount + "回";
      const duration = isOn && channel.onSinceMs !== null
        ? Math.max(0, elapsedMs - channel.onSinceMs)
        : 0;
      channel.durationValue.textContent = isOn
        ? formatDuration(duration)
        : "ON継続 0秒";
    });
  }

  function makeEventItem(event) {
    const item = document.createElement("div");
    item.className = "event-item";
    const time = document.createElement("time");
    time.className = "event-time mono";
    time.dateTime = event.at.toISOString();
    time.textContent = formatClock(event.at).slice(0, 5);
    const name = document.createElement("strong");
    name.className = "event-name demo-event-channel";
    name.textContent = event.channelId;
    const value = document.createElement("span");
    value.className = "value-pill value-pill--" + event.state.toLowerCase() + " demo-event-state";
    value.textContent = event.state;
    item.append(time, name, value);
    return item;
  }

  function renderHistory() {
    eventList.replaceChildren();
    eventTable.replaceChildren();
    const totalEvents = channels.reduce(function (total, channel) {
      return total + channel.onCount;
    }, 0);
    document.getElementById("event-count").textContent = String(totalEvents);
    document.getElementById("active-count").textContent = String(
      channels.filter(function (channel) { return channel.state === "ON"; }).length
    );
    document.getElementById("log-total").textContent = simulation.events.length + "件";

    if (simulation.events.length === 0) {
      const empty = document.createElement("p");
      empty.className = "custom-widget-empty demo-empty";
      empty.textContent = "対象期間の記録はありません。";
      eventList.append(empty);
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.textContent = "最初の信号変化を待機しています。";
      row.append(cell);
      eventTable.append(row);
      document.getElementById("last-event-channel").textContent = "入力変化を待機しています";
      document.getElementById("last-event-at").textContent = "--:--:--";
      return;
    }

    const latest = simulation.events[0];
    document.getElementById("last-event-channel").textContent =
      latest.channelId + " が " + latest.state;
    document.getElementById("last-event-at").textContent = formatClock(latest.at);

    simulation.events.forEach(function (event) {
      eventList.append(makeEventItem(event));
      const row = document.createElement("tr");
      const dateCell = document.createElement("td");
      const time = document.createElement("time");
      time.dateTime = event.at.toISOString();
      time.textContent = formatLongTime(event.at);
      dateCell.append(time);

      const channelCell = document.createElement("td");
      const channelName = document.createElement("strong");
      channelName.className = "demo-event-channel";
      channelName.textContent = event.channelId;
      channelCell.append(channelName);

      const stateCell = document.createElement("td");
      const state = document.createElement("span");
      state.className = "value-pill value-pill--" + event.state.toLowerCase();
      state.textContent = event.state;
      stateCell.append(state);

      const kindCell = document.createElement("td");
      const kind = document.createElement("span");
      kind.className = "kind-label";
      kind.textContent = "入力の変化";
      kindCell.append(kind);

      const qualityCell = document.createElement("td");
      const quality = document.createElement("span");
      quality.className = "sync-ok";
      quality.textContent = "デモ";
      qualityCell.append(quality);
      row.append(dateCell, channelCell, stateCell, kindCell, qualityCell);
      eventTable.append(row);
    });
  }

  function renderTimeline(elapsedMs) {
    const timeline = document.getElementById("timeline");
    timeline.replaceChildren();

    const axis = document.createElement("div");
    axis.className = "timeline-axis";
    ["信号", "60秒前", "45秒前", "30秒前", "15秒前", "現在"].forEach(function (label) {
      const item = document.createElement("span");
      item.textContent = label;
      axis.append(item);
    });
    timeline.append(axis);

    const endMs = Math.max(1, elapsedMs);
    const startMs = Math.max(0, endMs - 60000);
    const spanMs = Math.max(1, endMs - startMs);

    channels.forEach(function (channel) {
      const row = document.createElement("div");
      row.className = "timeline-row";
      const label = document.createElement("div");
      label.className = "timeline-label";
      const name = document.createElement("strong");
      name.textContent = channel.id;
      label.append(name);

      const track = document.createElement("div");
      track.className = "timeline-track";
      let state = channel.timelineStartState;
      let cursorMs = channel.timelineStartAtMs;
      if (cursorMs < startMs) cursorMs = startMs;

      simulation.timelineEvents.forEach(function (event) {
        if (event.channelId !== channel.id) return;
        if (event.atMs < channel.timelineStartAtMs) return;
        if (event.atMs < startMs) {
          state = event.state;
          cursorMs = startMs;
          return;
        }
        if (event.atMs > endMs) return;
        if (state === "ON" && event.atMs > cursorMs) {
          addTimelineSegment(track, cursorMs, event.atMs, startMs, spanMs);
        }
        state = event.state;
        cursorMs = event.atMs;
      });
      if (state === "ON" && endMs > cursorMs) {
        addTimelineSegment(track, cursorMs, endMs, startMs, spanMs);
      }
      row.append(label, track);
      timeline.append(row);
    });
  }

  function addTimelineSegment(track, fromMs, toMs, startMs, spanMs) {
    const left = Math.max(0, Math.min(100, ((fromMs - startMs) / spanMs) * 100));
    const right = Math.max(left, Math.min(100, ((toMs - startMs) / spanMs) * 100));
    if (right - left < 0.5) return;
    const segment = document.createElement("span");
    segment.className = "timeline-segment cyan";
    segment.style.left = left + "%";
    segment.style.width = (right - left) + "%";
    segment.setAttribute("aria-hidden", "true");
    track.append(segment);
  }

  function renderControls() {
    const running = simulation.running;
    loggingBadge.classList.toggle("is-stopped", !running);
    loggingStatus.textContent = running ? "記録中" : "停止中";
    realtimeStatus.textContent = running ? "自動切替中" : "一時停止中";
    realtimeStatus.classList.toggle("is-stopped", !running);
    simulationToggle.textContent = running ? "一時停止" : "再開";
    simulationToggle.setAttribute("aria-pressed", String(!running));
  }

  function renderDiagnostics() {
    const grid = document.getElementById("diagnostic-io-grid");
    grid.replaceChildren();
    channels.forEach(function (channel) {
      const cell = document.createElement("div");
      cell.className = "io-cell " + (channel.state === "ON" ? "is-on" : "is-off");
      const name = document.createElement("strong");
      name.textContent = channel.id;
      const value = document.createElement("small");
      value.textContent = channel.state;
      cell.append(name, value);
      grid.append(cell);
    });
  }

  function renderClock() {
    document.getElementById("system-clock").textContent = formatClock(new Date());
  }

  function render(elapsedMs, historyChanged) {
    renderClock();
    renderChannels(elapsedMs);
    renderControls();
    renderTimeline(elapsedMs);
    renderDiagnostics();
    if (historyChanged) renderHistory();
    else {
      document.getElementById("active-count").textContent = String(
        channels.filter(function (channel) { return channel.state === "ON"; }).length
      );
    }
  }

  function recordChange(change) {
    //予定された時刻と状態をイベントに保存し、遅延tick中も履歴を再現する。
    // 一時停止後は再開時の壁時計を新しい基準にして、停止時間を除外する。
    const event = {
      at: new Date(
        simulation.resumedAtEpochMs + change.atMs - simulation.elapsedBeforeResumeMs
      ),
      atMs: change.atMs,
      channelId: change.channel.id,
      state: change.state
    };
    simulation.events.unshift(event);
    simulation.timelineEvents.push(event);
    if (simulation.events.length > historyLimit) simulation.events.length = historyLimit;
    if (simulation.timelineEvents.length > timelineLimit) {
      simulation.timelineEvents.shift();
    }
  }

  function advanceSimulation(elapsedMs) {
    const changes = [];
    channels.forEach(function (channel) {
      while (elapsedMs >= channel.nextChangeAtMs) {
        channel.state = channel.state === "OFF" ? "ON" : "OFF";
        if (channel.state === "ON") {
          channel.onCount += 1;
          channel.onSinceMs = channel.nextChangeAtMs;
        } else {
          channel.onSinceMs = null;
        }
        // 変化した瞬間の値を保持し、後段の描画で最終状態に上書きされないようにする。
        changes.push({
          channel: channel,
          state: channel.state,
          atMs: channel.nextChangeAtMs
        });
        channel.nextChangeAtMs += channel.changeEveryMs;
      }
    });
    changes.sort(function (left, right) { return left.atMs - right.atMs; });
    changes.forEach(recordChange);
    return changes.length > 0;
  }

  function tick() {
    const elapsedMs = elapsedSimulationMs();
    // 停止中も時計表示は更新し、仮想時間と状態だけを固定する。
    const changed = simulation.running ? advanceSimulation(elapsedMs) : false;
    render(elapsedMs, changed);
  }

  channelGrid.replaceChildren.apply(channelGrid, channels.map(createChannelCard));
  render(elapsedSimulationMs(), true);

  document.querySelectorAll("[data-view]").forEach(function (button) {
    button.addEventListener("click", function () {
      showView(button.dataset.view);
    });
  });
  document.querySelectorAll("[data-view-link]").forEach(function (button) {
    button.addEventListener("click", function () {
      showView(button.dataset.viewLink);
    });
  });
  document.querySelectorAll("[data-settings-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      const targetId = button.dataset.settingsTarget;
      document.querySelectorAll("[data-settings-target]").forEach(function (item) {
        item.classList.toggle("is-active", item === button);
      });
      document.querySelectorAll(".settings-section").forEach(function (section) {
        section.classList.toggle("is-active-settings", section.id === targetId);
      });
    });
  });

  function showView(name) {
    document.querySelectorAll(".view").forEach(function (view) {
      view.classList.toggle("is-active", view.id === "view-" + name);
    });
    document.querySelectorAll("[data-view]").forEach(function (button) {
      button.classList.toggle("is-active", button.dataset.view === name);
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  simulationToggle.addEventListener("click", function () {
    if (simulation.running) {
      const elapsedMs = elapsedSimulationMs();
      const changed = advanceSimulation(elapsedMs);
      simulation.elapsedBeforeResumeMs = elapsedMs;
      simulation.running = false;
      render(elapsedMs, changed);
      return;
    }
    simulation.resumedAtMs = performance.now();
    simulation.resumedAtEpochMs = Date.now();
    simulation.running = true;
    render(elapsedSimulationMs(), false);
  });

  resetDemo.addEventListener("click", function () {
    const elapsedMs = elapsedSimulationMs();
    channels.forEach(function (channel) {
      channel.onCount = 0;
      channel.timelineStartAtMs = elapsedMs;
      channel.timelineStartState = channel.state;
    });
    simulation.events = [];
    simulation.timelineEvents = [];
    render(elapsedMs, true);
  });

  window.setInterval(tick, 250);
})();
