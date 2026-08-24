-- Row-level security: applied identically to every business table except
-- entities (the registry the scoping keys into).
-- Source of truth: docs/data-model.md §Row-level security.
--
-- Policies call app_entity_id() rather than reading the setting directly.
-- A connection that never scoped itself — or one whose scope was cleared —
-- gets a raised error on any query. Loud failure, never another entity's
-- rows, and never a silently empty result that would read as "nothing to
-- order".

CREATE FUNCTION app_entity_id() RETURNS TEXT
LANGUAGE plpgsql STABLE AS $$
DECLARE v TEXT := current_setting('app.entity_id', true);
BEGIN
  IF v IS NULL OR v = '' THEN
    RAISE EXCEPTION
      'app.entity_id is not set: refusing to run a query with no entity scope'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN v;
END $$;

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
        USING (entity_id = app_entity_id())
        WITH CHECK (entity_id = app_entity_id())
    $f$, t || '_entity_isolation', t);
  END LOOP;
END $$;
