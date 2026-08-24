-- Re-running a week's report is harmless: it reads and writes a file. The
-- guard that stops a week running twice exists for actions with an effect
-- outside this machine, and one rule covering both blocked the safe case.
--
-- A re-run therefore keeps both reports and records which replaced which,
-- so "why does this week have two reports" has an answer in the data.

ALTER TABLE reports
    ADD COLUMN superseded_by UUID REFERENCES reports(id),
    ADD COLUMN superseded_at TIMESTAMPTZ;

COMMENT ON COLUMN reports.superseded_by IS
    'The report that replaced this one, when a week was re-run. NULL for '
    'the current report. The superseded row is never deleted; the only '
    'edits it takes are these two columns and file_path, which is '
    'repointed at the renamed copy of its own report.';

-- now() is the transaction's start time, so two reports written inside one
-- transaction share a timestamp and cannot be told apart by age. Which of
-- two reports for a week is the current one is exactly the question this
-- migration exists to answer, so the clock has to move between them.
ALTER TABLE reports ALTER COLUMN created_at SET DEFAULT clock_timestamp();

CREATE INDEX reports_current_idx ON reports (entity_id, agent_kind, created_at DESC)
    WHERE superseded_by IS NULL;
