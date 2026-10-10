/* HTML表示用のエスケープ、信号選択、チャンネル名の整形。DOMには依存しない。 */
(() => {
  "use strict";

  function signalIds(value) {
    return String(value || "").split(",").filter(Boolean);
  }

  function channelLabel(channel) {
    const name = channel.name.trim();
    return `IN${channel.id}${name ? ` ${name}` : ""}${channel.enabled ? "" : "（未使用）"}`;
  }

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
