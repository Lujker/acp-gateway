-- Minimal single-owner identity, not a public multi-user account system.
CREATE TABLE pairing_owner (
 singleton INTEGER PRIMARY KEY CHECK(singleton = 1), owner_id TEXT NOT NULL,
 telegram_ids TEXT NOT NULL
);
CREATE TABLE pairing_requests (
 request_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, applicant_digest TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL, target TEXT, target_generation INTEGER, verification TEXT NOT NULL,
 expires REAL NOT NULL, state TEXT NOT NULL, computer_id TEXT,
 credential TEXT, generation INTEGER
);
CREATE INDEX pairing_expiry ON pairing_requests(expires);
CREATE TABLE pairing_bindings (
 computer_id TEXT PRIMARY KEY REFERENCES computers(computer_id), owner_id TEXT NOT NULL,
 generation INTEGER NOT NULL
);
CREATE TABLE pairing_audit (
 id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, event TEXT NOT NULL,
 actor TEXT NOT NULL, at REAL NOT NULL
);
