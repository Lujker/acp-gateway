CREATE TABLE computer_connections (
    computer_id TEXT PRIMARY KEY REFERENCES computers(computer_id) ON DELETE CASCADE,
    epoch TEXT NOT NULL,
    connected_at TEXT NOT NULL,
    disconnected_at TEXT,
    disconnect_reason TEXT,
    close_code INTEGER,
    agents_json TEXT NOT NULL
);
