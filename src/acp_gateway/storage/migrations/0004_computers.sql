CREATE TABLE computers (
    computer_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    credential_digest TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    generation INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
