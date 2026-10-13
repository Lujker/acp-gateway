-- Bound each retention sweep and avoid scans of recent history.
CREATE INDEX jobs_by_finished_at ON jobs(finished_at, id);
CREATE INDEX approvals_by_resolved_at ON approvals_audit(resolved_at, id);
