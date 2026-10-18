// Sites上のUI確認専用fixture。製品側のDomainや実機状態とは接続しない。
window.DEMO_FIXTURES = {
// デモ表示の期間・イベントは固定サンプル。実機の時計やDBとは連動しない。
  dashboardPeriods: {
    "1h": { label: "直近1時間", durationMs: 3600000, count: 8, onTime: "42分", lastAlarm: "該当なし", axis: ["10:45", "11:00", "11:15", "11:30", "11:45"] },
    "8h": { label: "直近8時間", durationMs: 28800000, count: 128, onTime: "4時間12分", lastAlarm: "10:31:22", axis: ["04:00", "06:00", "08:00", "10:00", "12:00"] },
    "24h": { label: "直近24時間", durationMs: 86400000, count: 246, onTime: "9時間35分", lastAlarm: "10:31:22", axis: ["昨日12時", "18時", "今日0時", "6時", "12時"] },
    "7d": { label: "直近7日", durationMs: 7 * 86400000 },
    "30d": { label: "直近30日", durationMs: 30 * 86400000 },
    "today": { label: "今日", startAt: "2026-09-12T00:00:00+09:00", count: 196, onTime: "7時間08分", lastAlarm: "10:31:22", axis: ["0時", "3時", "6時", "9時", "12時"] },
    "yesterday": { label: "昨日", startAt: "2026-09-11T00:00:00+09:00", endAt: "2026-09-12T00:00:00+09:00", axis: ["0時", "6時", "12時", "18時", "24時"] },
    "this_week": { label: "今週", startAt: "2026-09-07T00:00:00+09:00", axis: ["月", "火", "水", "木", "金", "土"] },
    "custom": { label: "任意期間", startAt: "2026-09-12T08:00:00+09:00", endAt: "2026-09-12T12:00:00+09:00" }
  },
// HAT入力6chと出力6chの画面例を提供する。
  channels: [
    { id: 1, name: "運転", enabled: true, value: "ON", debounce: 20 },
    { id: 2, name: "異常", enabled: true, value: "OFF", debounce: 50 },
    { id: 3, name: "完了", enabled: true, value: "OFF", debounce: 10 },
    { id: 4, name: "扉", enabled: true, value: "OFF", debounce: 100 },
    { id: 5, name: "", enabled: false, value: "UNKNOWN", debounce: 20 },
    { id: 6, name: "", enabled: false, value: "UNKNOWN", debounce: 20 }
  ],
  outputs: [
    { id: 1, name: "Heartbeat", value: "ON", owner: "Heartbeat" },
    { id: 2, name: "長時間停止", value: "OFF", owner: "条件ルール" },
    { id: 3, name: "異常継続", value: "OFF", owner: "条件ルール" },
    { id: 4, name: "未使用", value: "OFF", owner: "未使用" },
    { id: 5, name: "未使用", value: "OFF", owner: "未使用" },
    { id: 6, name: "未使用", value: "OFF", owner: "未使用" }
  ],
  settings: {
    deviceName: "第2組立機",
    hostname: "event-logger-a3f2",
    timezone: "Asia/Tokyo",
    passwordProtection: false,
    setupCompleted: true,
    heartbeat: { enabled: true, outputId: 1, intervalSeconds: 5 },
    retentionDays: 365
  },
  counterResets: {
    "cycle-count": []
  },
  outputRules: [
    { id: 1, name: "長時間停止", condition: "state_duration", channelId: "IN1", value: "OFF", edge: "ON", duration: 30, durationUnit: "minutes", outputId: 2, enabled: true },
    { id: 2, name: "異常継続", condition: "state_duration", channelId: "IN2", value: "ON", edge: "ON", duration: 5, durationUnit: "minutes", outputId: 3, enabled: true }
  ],
// タイムライン・ログ一覧を表示するための疑似イベント。
  events: [
    { time: "11:43:40.000", signal: "OUT1", value: "ON", kind: "output", source: "heartbeat", synced: true },
    { time: "11:43:35.000", signal: "OUT1", value: "OFF", kind: "output", source: "heartbeat", synced: true },
    { time: "11:42:58.418", signal: "IN3", value: "OFF", kind: "edge", source: "", synced: true },
    { time: "11:42:58.203", signal: "IN3", value: "ON", kind: "edge", source: "", synced: true },
    { time: "11:36:12.901", signal: "IN1", value: "ON", kind: "edge", source: "", synced: true },
    { time: "11:31:44.520", signal: "IN1", value: "OFF", kind: "edge", source: "", synced: true },
    { time: "11:24:31.182", signal: "IN3", value: "OFF", kind: "edge", source: "", synced: true },
    { time: "11:24:30.982", signal: "IN3", value: "ON", kind: "edge", source: "", synced: true },
    { time: "10:36:52.114", signal: "IN2", value: "OFF", kind: "edge", source: "", synced: true },
    { time: "10:31:22.418", signal: "IN2", value: "ON", kind: "edge", source: "", synced: true },
    { time: "08:03:11.218", signal: "IN1", value: "ON", kind: "snapshot", source: "", synced: false },
    { time: "08:03:11.218", signal: "IN2", value: "OFF", kind: "snapshot", source: "", synced: false },
    { time: "08:03:11.218", signal: "IN3", value: "OFF", kind: "snapshot", source: "", synced: false }
  ],
  timeline: [
    { id: "IN1", color: "cyan", segments: [[0, 12], [18, 39], [44, 68], [74, 100]] },
    { id: "IN2", color: "red", segments: [[56, 63]] },
    { id: "IN3", color: "amber", pulses: [7, 16, 25, 34, 47, 54, 66, 78, 88, 96] },
    { id: "IN4", color: "violet", segments: [[29, 34], [69, 73]] },
    { id: "OUT1", color: "green", source: "heartbeat", segments: [[4, 8], [12, 16], [20, 24], [28, 32], [36, 40], [44, 48], [52, 56], [60, 64], [68, 72], [76, 80], [84, 88], [92, 96]] }
  ],
  health: [
    { label: "App version", value: "0.1.0", state: "ok" },
    { label: "logger状態", value: "動作中", state: "ok" },
    { label: "DB使用量", value: "82.4 MB", state: "ok" },
    { label: "SD総容量", value: "59.5 GB", state: "ok" },
    { label: "SD使用量", value: "19.0 GB", state: "ok" },
    { label: "SD空き容量", value: "40.5 GB", state: "ok" },
    { label: "SD空き率", value: "68%", state: "ok" },
    { label: "ストレージ状態", value: "正常", state: "ok" },
    { id: "diagnostic-clock", label: "システム時刻", value: "--", state: "ok" },
    { label: "NTP同期状態", value: "同期済み", state: "ok" },
    { label: "Heartbeat状態", value: "OUT1 · 5秒", state: "ok" }
  ]
};
