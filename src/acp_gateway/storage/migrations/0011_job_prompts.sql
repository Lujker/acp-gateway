-- New prompts belong to their jobs and follow the same retention policy.
-- Old jobs have no row: never invent a prompt from a title or an answer.
CREATE TABLE job_prompts (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    prompt TEXT NOT NULL
);
CREATE INDEX jobs_dialogue_history ON jobs(session_id, created_at, id);
