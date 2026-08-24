-- 0004_entity_scope_function.sql — make an unset scope fail loudly again.
--
-- 0002 relied on the one-argument current_setting('app.entity_id') raising
-- when no scope was set. It does, but only until the first `SET LOCAL` on
-- that connection: afterwards Postgres remembers the setting as the empty
-- string, so the next unscoped query matches nothing and quietly returns
-- zero rows. Zero rows reads as "nothing to order", which is the one wrong
-- answer this scheme exists to prevent.
--
-- app_entity_id() raises instead, with the same SQLSTATE (42704,
-- undefined_object) the bare setting raised before, so an unscoped query
-- fails the same way on the first call and on the thousandth.

CREATE FUNCTION app_entity_id() RETURNS text
  LANGUAGE plpgsql
  STABLE
AS $$
DECLARE scope TEXT := current_setting('app.entity_id', true);
BEGIN
  IF scope IS NULL OR scope = '' THEN
    RAISE EXCEPTION
      'No business is in scope: app.entity_id is unset for this transaction'
      USING ERRCODE = '42704',
            HINT = 'Run the query inside entity_session(conn, entity_id).';
  END IF;
  RETURN scope;
END $$;

REVOKE ALL ON FUNCTION app_entity_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_entity_id() TO agent_org_app;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
      'suppliers','components','products','boms','channels','tasks',
      'action_proposals','approvals','audit_log','agent_runs','order_history',
      'reports']
  LOOP
    EXECUTE format('DROP POLICY %I ON %I', t || '_entity_isolation', t);
    EXECUTE format($f$
      CREATE POLICY %I ON %I
        USING (entity_id = app_entity_id())
        WITH CHECK (entity_id = app_entity_id())
    $f$, t || '_entity_isolation', t);
  END LOOP;
END $$;
