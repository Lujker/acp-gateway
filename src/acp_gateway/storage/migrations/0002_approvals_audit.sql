-- Only settled decisions survive a restart; pending RPCs are kept in memory.
CREATE TABLE approvals_audit (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs (id) ON DELETE SET NULL,
    channel TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    agent TEXT NOT NULL,
    acp_session_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    title TEXT,
    kind TEXT,
    raw_input TEXT,
    requested_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    option_id TEXT,
    decided_channel TEXT,
    actor TEXT,
    reason TEXT
);
CREATE INDEX approvals_by_job ON approvals_audit (job_id);
