-- Bounded by the request lifetime; no applicant secrets or issued credentials.
CREATE TABLE pairing_notifications (
 request_id TEXT NOT NULL REFERENCES pairing_requests(request_id) ON DELETE CASCADE,
 namespace TEXT NOT NULL, actor TEXT NOT NULL, state TEXT NOT NULL,
 retry_at REAL NOT NULL, token TEXT UNIQUE, message_id INTEGER,
 PRIMARY KEY (request_id, namespace, actor)
);
