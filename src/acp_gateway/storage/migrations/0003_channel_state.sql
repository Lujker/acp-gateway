-- Small durable cursors/preferences; never store bot tokens here.
CREATE TABLE channel_state (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);
