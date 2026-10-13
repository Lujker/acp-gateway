CREATE TABLE outbound_deliveries (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting'
      CHECK(state IN ('waiting','pending','sending','delivered','unknown','failed')),
    next_chunk INTEGER NOT NULL DEFAULT 0 CHECK(next_chunk >= 0),
    content_hash TEXT,
    retry_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX outbound_due ON outbound_deliveries(namespace, state, retry_at);
-- Job completion and outbound readiness commit in the same SQLite statement.
CREATE TRIGGER outbound_job_finished AFTER UPDATE OF status ON jobs
WHEN NEW.status != 'running'
BEGIN
    UPDATE outbound_deliveries SET state = 'pending'
    WHERE job_id = NEW.id AND state = 'waiting';
END;
