-- A source snapshot belongs to the turn, not to the current route selection.
CREATE TABLE job_context (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    context TEXT NOT NULL
);
-- Pin Telegram presentation before binding a digest or acknowledging any chunk.
ALTER TABLE outbound_deliveries ADD COLUMN presentation TEXT;
