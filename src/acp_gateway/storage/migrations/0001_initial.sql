-- Sessions bound to channel conversations, the active session of each
-- conversation, and jobs (prompt turns). Prompts are not stored; answers are,
-- so a job result survives a gateway restart.

CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    agent TEXT NOT NULL,
    acp_session_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    UNIQUE (agent, acp_session_id)
);

CREATE INDEX sessions_by_conversation ON sessions (channel, conversation_key, agent);

CREATE TABLE conversations (
    channel TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    agent TEXT NOT NULL,
    active_session_id INTEGER REFERENCES sessions (id) ON DELETE SET NULL,
    PRIMARY KEY (channel, conversation_key, agent)
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    stop_reason TEXT,
    error TEXT,
    usage TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX jobs_by_session ON jobs (session_id);
CREATE INDEX jobs_by_status ON jobs (status);
