/* Web画面の日時解析・表記。現在の設定タイムゾーンをcallbackで取得する。 */
(() => {
  "use strict";

  function createTimeFormatter(selectedTimeZone) {
    // 本体設定の変更後も、その時点の表示タイムゾーンを参照する。
  function timeZoneOffset() {
    return selectedTimeZone() === "UTC" ? "+00:00" : "+09:00";
  }

  function parseLocalDateTime(value) {
    if (!value) return Number.NaN;
    return Date.parse(`${value}:00${timeZoneOffset()}`);
  }

  function dateTimeParts(timestampMs) {
    const formatter = new Intl.DateTimeFormat("ja-JP", {
      timeZone: selectedTimeZone(), year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
    });
    return Object.fromEntries(formatter.formatToParts(new Date(timestampMs)).map((part) => [part.type, part.value]));
  }

  function formatLocalDateTime(timestampMs, includeMilliseconds = false) {
    const parts = dateTimeParts(timestampMs);
    const milliseconds = includeMilliseconds ? `.${String(new Date(timestampMs).getUTCMilliseconds()).padStart(3, "0")}` : "";
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}${milliseconds}${timeZoneOffset()}`;
  }

  function formatDateTimeInput(timestampMs) {
    const parts = dateTimeParts(timestampMs);
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  }

    return Object.freeze({
      timeZoneOffset,
      parseLocalDateTime,
      dateTimeParts,
      formatLocalDateTime,
      formatDateTimeInput
    });
  }

  window.EventLoggerTimeFormat = Object.freeze({ createTimeFormatter });
})();
