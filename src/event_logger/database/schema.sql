CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    boot_id TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER,
    app_version TEXT NOT NULL,
    clock_synced INTEGER NOT NULL DEFAULT 0 CHECK(clock_synced IN (0, 1))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    ts_utc_ms INTEGER NOT NULL,
    mono_ns INTEGER NOT NULL,
    channel_id INTEGER NOT NULL CHECK(channel_id BETWEEN 1 AND 6),
    value INTEGER NOT NULL CHECK(value IN (0, 1)),
    kind INTEGER NOT NULL CHECK(kind IN (0, 1)),
    clock_synced INTEGER NOT NULL DEFAULT 0 CHECK(clock_synced IN (0, 1)),
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS output_events (
    id INTEGER PRIMARY KEY,
    session_id INTEGER,
    ts_utc_ms INTEGER NOT NULL,
    mono_ns INTEGER NOT NULL,
    output_channel INTEGER NOT NULL CHECK(output_channel BETWEEN 1 AND 6),
    value INTEGER NOT NULL CHECK(value IN (0, 1)),
    source TEXT NOT NULL CHECK(source IN ('rule', 'heartbeat', 'test', 'startup')),
    clock_synced INTEGER NOT NULL DEFAULT 0 CHECK(clock_synced IN (0, 1)),
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_utc_ms);
CREATE INDEX IF NOT EXISTS idx_events_channel_ts ON events(channel_id, ts_utc_ms);
CREATE INDEX IF NOT EXISTS idx_events_counter
    ON events(channel_id, value, kind, id);
CREATE INDEX IF NOT EXISTS idx_events_edge_count
    ON events(channel_id, value, kind, ts_utc_ms);
CREATE INDEX IF NOT EXISTS idx_events_clock_synced ON events(clock_synced);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id, id);
CREATE INDEX IF NOT EXISTS idx_output_events_ts ON output_events(ts_utc_ms);
CREATE INDEX IF NOT EXISTS idx_output_events_channel_ts
    ON output_events(output_channel, ts_utc_ms);
CREATE INDEX IF NOT EXISTS idx_output_events_clock_synced
    ON output_events(clock_synced);
CREATE INDEX IF NOT EXISTS idx_output_events_session_id
    ON output_events(session_id, id);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY CHECK(id BETWEEN 1 AND 6),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    name TEXT NOT NULL,
    debounce_ms INTEGER NOT NULL DEFAULT 20 CHECK(debounce_ms BETWEEN 0 AND 5000),
    on_label TEXT NOT NULL DEFAULT 'ON',
    off_label TEXT NOT NULL DEFAULT 'OFF',
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS widgets (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    config_json TEXT NOT NULL,
    size TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS counter_resets (
    id INTEGER PRIMARY KEY,
    widget_id TEXT NOT NULL,
    reset_at_ms INTEGER NOT NULL,
    after_event_id INTEGER NOT NULL,
    carried_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(widget_id) REFERENCES widgets(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS output_rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    rule_type TEXT NOT NULL,
    config_json TEXT NOT NULL,
    output_channel INTEGER NOT NULL CHECK(output_channel BETWEEN 1 AND 6),
    updated_at_ms INTEGER NOT NULL,
    UNIQUE(output_channel)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts_utc_ms INTEGER NOT NULL,
    action TEXT NOT NULL,
    details_json TEXT
);
