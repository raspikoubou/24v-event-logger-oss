/* ストレージの警告判定・表示文生成。DOM・通信・loggerd制御とは独立。 */
(() => {
  "use strict";

  // 保存容量の値を利用者が読みやすい単位へ変換する。
  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
  }

  // 測定失敗と実際の容量逼迫を区別して警告情報を構築する。
  function storageWarningFor(storage, hasError = false) {
    const status = storage?.status;
    if (!hasError && status !== "warning" && status !== "critical") {
      return { visible: false, severity: "", title: "", freeBytes: null };
    }
    const severity = hasError || status === "critical" ? "critical" : "warning";
    const rawFreeBytes = storage?.free_bytes;
    // nullや空文字は未計測値であり、0バイトの実測とは区別する。
    const hasMeasurement = (
      (typeof rawFreeBytes === "number"
        || (typeof rawFreeBytes === "string" && rawFreeBytes.trim() !== ""))
      && Number.isFinite(Number(rawFreeBytes))
    );
    return {
      visible: true,
      severity,
      title: hasError
        ? "ストレージ状態を確認できません"
        : severity === "critical"
          ? "ストレージ容量が不足しています"
          : "ストレージの空き容量が少なくなっています",
      freeBytes: !hasError && hasMeasurement
        ? Math.max(0, Number(rawFreeBytes))
        : null
    };
  }

  // 診断状態から利用者向けの注意文を選ぶ。
  function storageWarningMessage(warning) {
    if (warning.severity === "critical") {
      const remaining = warning.freeBytes === null
        ? "空き容量を確認してください。"
        : `残り ${formatBytes(warning.freeBytes)}です。`;
      return `ログ保存を継続できなくなる可能性があります。${remaining}`;
    }
    const remaining = warning.freeBytes === null
      ? "空き容量を確認してください。"
      : `残り ${formatBytes(warning.freeBytes)}です。`;
    return remaining + "空き容量が30%未満です。診断画面で確認してください。";
  }

  // 警告が表示済みか・現在の状態に応じて表示の要否を判定する。
  function warningVisibilityFor({
    count,
    latencyWarning,
    configurationWarning,
    storageVisible
  }) {
    const syncVisible = count > 0 || Boolean(latencyWarning) || Boolean(configurationWarning);
    return { syncVisible, visible: syncVisible || Boolean(storageVisible) };
  }

  window.EventLoggerStoragePolicy = Object.freeze({
    formatBytes,
    storageWarningFor,
    storageWarningMessage,
    warningVisibilityFor
  });
})();
