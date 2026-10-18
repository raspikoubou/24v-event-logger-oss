/* Web画面の日時解析・表記。現在の設定タイムゾーンをcallbackで取得する。 */
(() => {
  "use strict";

  /**
   * 指定timezoneに基づく日時formatterを作る。
   * @param {string} selectedTimeZone - 日時表示に使うtimezone。
   * @returns {Object} timezone依存のtimeZoneOffset、parseLocalDateTime、dateTimeParts、formatLocalDateTime、formatDateTimeInput関数を持つ凍結object。
   */
  function createTimeFormatter(selectedTimeZone) {
    // 本体設定の変更後も、その時点の表示タイムゾーンを参照する。
  /**
   * 現在のtimezone offsetを表示用文字列へ変換する。
   * @returns {string} datetime-localをDate.parseへ渡すoffset文字列。UTCでは+00:00、それ以外は現行実装の+09:00。
   */
  function timeZoneOffset() {
    return selectedTimeZone() === "UTC" ? "+00:00" : "+09:00";
  }

  /**
   * datetime-local値をUTC millisecondへ変換する。
   * @param {string} value - 変換対象の値。
   * @returns {number} datetime-local文字列をUTC Unix epoch millisecondへ変換する。空値はNaN。
   */
  function parseLocalDateTime(value) {
    if (!value) return Number.NaN;
    return Date.parse(`${value}:00${timeZoneOffset()}`);
  }

  /**
   * timestampを指定timezoneの年月日時分秒部品へ分解する。
   * @param {number} timestampMs - UTC Unix epoch millisecond。
   * @returns {Object} 指定timezoneのyear/month/day/hour/minute/second文字列を持つobject。
   */
  function dateTimeParts(timestampMs) {
    const formatter = new Intl.DateTimeFormat("ja-JP", {
      timeZone: selectedTimeZone(), year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
    });
    return Object.fromEntries(formatter.formatToParts(new Date(timestampMs)).map((part) => [part.type, part.value]));
  }

  /**
   * timestampを選択timezoneの日時文字列へ変換する。
   * @param {number} timestampMs - UTC Unix epoch millisecond。
   * @param {boolean} includeMilliseconds - 日時表示にmillisecondを含めるか。
   * @returns {string} 指定timezoneのYYYY-MM-DD HH:mm:ss±HH:MM文字列。要求時はUTC millisecondも付加する。
   */
  function formatLocalDateTime(timestampMs, includeMilliseconds = false) {
    const parts = dateTimeParts(timestampMs);
    const milliseconds = includeMilliseconds ? `.${String(new Date(timestampMs).getUTCMilliseconds()).padStart(3, "0")}` : "";
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}${milliseconds}${timeZoneOffset()}`;
  }

  /**
   * timestampをdatetime-local input向け文字列へ変換する。
   * @param {number} timestampMs - UTC Unix epoch millisecond。
   * @returns {string} datetime-localへ設定できるYYYY-MM-DDTHH:mm文字列。
   */
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
