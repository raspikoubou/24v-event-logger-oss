/* 記録保持期間のUI/API変換。無期限はnull、値未設定は既定の365日を維持する。 */
(() => {
  "use strict";

  // 「無期限」はAPI/DBのnullで表す。selectの空値をNumber()で0へ変換しない。
  function retentionDaysFromSelectValue(value) {
    return value === "" ? null : Number(value);
  }

  function retentionDaysFromStatus(status) {
    return status.retention_days === undefined ? 365 : status.retention_days;
  }

  window.EventLoggerRetentionPolicy = Object.freeze({
    retentionDaysFromSelectValue,
    retentionDaysFromStatus
  });
})();
