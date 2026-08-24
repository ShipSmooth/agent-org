-- 0008 — two facts that have to outlive a process: whether a report was
-- emailed, and what has already been proposed against a hand count.
--
-- Both are here rather than in the reports row because both answer a
-- different question from "what did this week say".

-- Sending is recorded apart from the report itself, so a resend is never
-- mistaken for a re-run. A re-run makes a new report; a resend makes a new
-- row here against the same one. A failed send is a row too, with the
-- reason in it: a run that produced a report and could not deliver it is
-- not a run that produced nothing, and SMTP having a bad minute must not
-- look like a week that never happened.
CREATE TABLE report_emails (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    report_id     UUID NOT NULL REFERENCES reports(id),
    recipients    TEXT NOT NULL,
    subject       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('SENT', 'FAILED')),
    error         TEXT,
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE report_emails IS
    'Every attempt to email a report, successful or not. Separate from '
    'reports so that "was this week delivered?" and "what did this week '
    'say?" are separate questions with separate answers.';

CREATE INDEX report_emails_report_idx ON report_emails (entity_id, report_id, attempted_at DESC);

-- Eleven components are counted by hand on a shelf rather than tracked in
-- Veeqo. Their count does not fall when stock is used, so a proposal
-- against an unchanged count would repeat every Monday forever. This table
-- remembers what was already proposed against which count, which is what
-- makes "not repeating it" survive a restart, a new week and a --again.
--
-- The key is the count date, not the report: re-running the same week must
-- suppress the repeat too, and a genuinely new count is a new date and
-- proposes again.
CREATE TABLE manual_stock_proposals (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    supplier      TEXT NOT NULL,
    part          TEXT NOT NULL,
    counted_on    DATE NOT NULL,
    count_units   INT NOT NULL,
    proposed_units INT NOT NULL,
    report_id     UUID REFERENCES reports(id),
    proposed_on   DATE NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (entity_id, supplier, part, counted_on)
);

COMMENT ON TABLE manual_stock_proposals IS
    'What Shannon has already asked Zach to buy for a hand-counted '
    'component, against the count that was current when she asked. One row '
    'per component per count date: the ON CONFLICT DO NOTHING that writes '
    'it is what stops a weekly repeat, including on a deliberate re-run.';

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['report_emails', 'manual_stock_proposals']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($f$
      CREATE POLICY %I ON %I
        USING (entity_id = current_setting('app.entity_id'))
        WITH CHECK (entity_id = current_setting('app.entity_id'))
    $f$, t || '_entity_isolation', t);
  END LOOP;
END $$;

GRANT SELECT, INSERT ON report_emails, manual_stock_proposals TO agent_org_app;
