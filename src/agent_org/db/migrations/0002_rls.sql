-- 0002_rls.sql — entity isolation, enforced by Postgres rather than by
-- remembering to write WHERE entity_id = ... in application code.
--
-- current_setting('app.entity_id') is deliberately the one-argument form: a
-- connection that never ran `SET LOCAL app.entity_id` gets an error on any
-- query. Loud failure, never another entity's rows and never a silently
-- empty result.

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
      'suppliers','components','products','boms','channels','tasks',
      'action_proposals','approvals','audit_log','agent_runs','order_history',
      'reports']
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
