CREATE TABLE model_state (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN
        ('intent', 'pending', 'unknown', 'observed', 'refused', 'confirmation_required')),
    requested_provider TEXT,
    requested_model TEXT,
    selection_revision INTEGER,
    observed_provider TEXT,
    observed_model TEXT,
    observation_source TEXT,
    deferred INTEGER NOT NULL DEFAULT 0,
    turn_completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX model_state_unresolved ON model_state(state);
