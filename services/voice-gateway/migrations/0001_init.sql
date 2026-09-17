-- IMP-03a 0001：控制核心最小持久化（计划 §4.6）。
-- 原则：MA 是播放器真实状态的唯一事实来源；本库只存「命令、授权、会话」
-- 这些属于本系统的状态，不复制 MA 的曲库或 now_playing。

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  device_id   TEXT PRIMARY KEY,          -- 服务端签发的设备标识
  name        TEXT,
  kind        TEXT,                      -- pwa / voice-client / satellite / api
  token_hash  TEXT,                      -- 令牌哈希（IMP-04 落地，先占位）
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS player_bindings (
  scope        TEXT PRIMARY KEY,         -- 'default' 或 device_id
  player_id    TEXT NOT NULL,
  player_name  TEXT,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
  command_id    TEXT PRIMARY KEY,        -- uuid4 hex
  device_id     TEXT NOT NULL,
  request_id    TEXT NOT NULL,
  action        TEXT NOT NULL,
  args_json     TEXT,
  player_id     TEXT,
  intent_epoch  INTEGER NOT NULL,
  status        TEXT NOT NULL,           -- contracts.COMMAND_STATUSES
  result_json   TEXT,
  content_fp    TEXT NOT NULL,           -- 内容指纹（幂等冲突判定）
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
-- 幂等唯一约束（计划 §4.3）：同设备 + 同 request_id 只能有一条命令。
CREATE UNIQUE INDEX IF NOT EXISTS idx_commands_device_request
  ON commands (device_id, request_id);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands (status);
CREATE INDEX IF NOT EXISTS idx_commands_epoch ON commands (intent_epoch);

CREATE TABLE IF NOT EXISTS conversation_sessions (
  session_id   TEXT PRIMARY KEY,         -- 与 jsonl SessionStore 的 id 一致
  user_id      TEXT NOT NULL,
  scene        TEXT,
  goal         TEXT,
  constraints_json TEXT,
  created_at   TEXT NOT NULL,
  last_active  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listening_sessions (
  listening_session_id TEXT PRIMARY KEY, -- uuid4 hex
  device_id    TEXT,
  player_id    TEXT NOT NULL,
  scene        TEXT,
  status       TEXT NOT NULL,            -- active / paused / ended / suspended
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  end_policy_json TEXT                    -- 定时结束（IMP-11：end_at/timer_revision）
);

CREATE TABLE IF NOT EXISTS feedback_events (
  event_id    TEXT PRIMARY KEY,          -- SessionStore.add_feedback 生成的 uuid
  device_id   TEXT,
  session_id  TEXT,
  signal      TEXT NOT NULL,
  target_json TEXT,
  note        TEXT,
  t_epoch     REAL,
  scope       TEXT NOT NULL DEFAULT 'public',
  source      TEXT NOT NULL DEFAULT 'event',   -- event / legacy
  legacy_uncertain INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feedback_identity
  ON feedback_events (signal, target_json);
