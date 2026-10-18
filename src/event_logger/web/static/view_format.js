/* HTML表示用のエスケープ、信号選択、チャンネル名の整形。DOMには依存しない。 */
(() => {
  "use strict";

  /**
   * 入力値から重複しないsignal ID列を解析する。
   * @param {string} value - 変換対象の値。
   * @returns {Array<string>} 重複を除いたsignal ID列。IN/OUTの正規形へ変換して返す。
   */
  function signalIds(value) {
    return String(value || "").split(",").filter(Boolean);
  }

  /**
   * channel設定を画面表示labelへ変換する。
   * @param {Object} channel - channel設定。
   * @returns {string} channel番号と設定名を含む表示label。
   */
  function channelLabel(channel) {
    const name = channel.name.trim();
    return `IN${channel.id}${name ? ` ${name}` : ""}${channel.enabled ? "" : "（未使用）"}`;
  }

  /**
   * HTML挿入前の文字列をescapeする。
   * @param {string} value - 変換対象の値。
   * @returns {string} HTML本文へ安全に挿入できるescape済み文字列。
   */
  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  window.EventLoggerViewFormat = Object.freeze({
    signalIds,
    channelLabel,
    escapeHtml
  });
})();
