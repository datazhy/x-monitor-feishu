-- ============================================================
-- SQLite schema:  X 推文监控 -> 飞书推送
-- 全部表都建唯一索引/幂等键，轮询模式必然会重复拿到推文。
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 推文主表：tweet_id 唯一，去重核心
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id        TEXT PRIMARY KEY,          -- X 推文 id，唯一去重键
    author_handle   TEXT NOT NULL,
    author_name     TEXT,
    author_user_id  TEXT,                       -- 保存 user_id，防 handle 改名失效
    text            TEXT,
    tweet_url       TEXT,
    is_reply        INTEGER DEFAULT 0,
    is_retweet      INTEGER DEFAULT 0,
    is_quote        INTEGER DEFAULT 0,
    media_count     INTEGER DEFAULT 0,
    created_at      TEXT,                        -- 推文发布时间 (ISO8601)
    received_at     TEXT NOT NULL,               -- 我方收到时间
    pushed_at       TEXT,                        -- 成功推送飞书时间
    source          TEXT DEFAULT 'webhook'       -- webhook | backfill
);
CREATE INDEX IF NOT EXISTS idx_tweets_handle ON tweets(author_handle);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at);

-- webhook 投递记录：按 delivery_id 幂等，防 webhook 重试重复处理
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id     TEXT PRIMARY KEY,            -- 来自请求头/请求体；无则用内容哈希
    received_at     TEXT NOT NULL,
    status          TEXT NOT NULL,               -- received | processed | error
    tweet_count     INTEGER DEFAULT 0,
    note            TEXT
);

-- 推送任务队列：每条推文一条，带重试状态
CREATE TABLE IF NOT EXISTS push_jobs (
    tweet_id        TEXT PRIMARY KEY REFERENCES tweets(tweet_id),
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | sending | done | dead
    retry_count     INTEGER DEFAULT 0,
    last_error      TEXT,
    next_retry_at   TEXT,                        -- ISO8601；到点才重试
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pushjobs_status ON push_jobs(status, next_retry_at);

-- dead-letter：彻底失败的推送，供人工排查 + 告警
CREATE TABLE IF NOT EXISTS dead_letters (
    tweet_id        TEXT PRIMARY KEY,
    last_error      TEXT,
    failed_at       TEXT NOT NULL,
    alerted         INTEGER DEFAULT 0
);

-- 规则状态：本地镜像，同步到 TwitterAPI.io
CREATE TABLE IF NOT EXISTS rules (
    rule_name        TEXT PRIMARY KEY,
    remote_rule_id   TEXT,                       -- TwitterAPI.io 返回的规则 id
    value            TEXT NOT NULL,              -- from:a OR from:b ...
    interval_seconds INTEGER NOT NULL,
    active           INTEGER DEFAULT 1,
    updated_at       TEXT NOT NULL
);

-- 每个博主"最后看到的 tweet_id"（去重规则：持久记录，重启不丢）
CREATE TABLE IF NOT EXISTS author_state (
    handle          TEXT PRIMARY KEY,
    last_tweet_id   TEXT,
    updated_at      TEXT NOT NULL
);

-- 被监控博主：保存 user_id 以便 handle 改名检测
CREATE TABLE IF NOT EXISTS handles (
    handle          TEXT PRIMARY KEY,
    user_id         TEXT,
    display_name    TEXT,
    last_checked_at TEXT,
    active          INTEGER DEFAULT 1
);

-- 每日成本/用量快照
CREATE TABLE IF NOT EXISTS cost_snapshots (
    day             TEXT PRIMARY KEY,            -- YYYY-MM-DD
    rule_count      INTEGER,
    api_calls       INTEGER,
    tweets_returned INTEGER,
    balance_usd     REAL,
    recorded_at     TEXT NOT NULL
);

-- 昨日信号早报：每日主题统计（趋势记忆/连续信号用）
CREATE TABLE IF NOT EXISTS daily_topics (
    day             TEXT NOT NULL,               -- 北京日期 YYYY-MM-DD
    topic           TEXT NOT NULL,
    tweet_count     INTEGER DEFAULT 0,
    author_count    INTEGER DEFAULT 0,
    PRIMARY KEY (day, topic)
);

-- 昨日信号早报：每日成稿存档（记忆/幂等/回看）
CREATE TABLE IF NOT EXISTS daily_reports (
    day             TEXT PRIMARY KEY,            -- 北京日期 YYYY-MM-DD
    report_json     TEXT,
    created_at      TEXT NOT NULL
);

-- LLM 实际 token 用量（成本核算）：按北京日期 + 模型聚合
CREATE TABLE IF NOT EXISTS llm_usage (
    day                 TEXT NOT NULL,           -- 北京日期 YYYY-MM-DD
    model               TEXT NOT NULL,
    calls               INTEGER DEFAULT 0,
    prompt_tokens       INTEGER DEFAULT 0,
    completion_tokens   INTEGER DEFAULT 0,
    PRIMARY KEY (day, model)
);

-- 运维事件日志（心跳、补漏、告警都写这里）
CREATE TABLE IF NOT EXISTS ops_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,                   -- heartbeat | backfill | alert | webhook | cost
    ok          INTEGER NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ops_kind ON ops_events(kind, created_at);
