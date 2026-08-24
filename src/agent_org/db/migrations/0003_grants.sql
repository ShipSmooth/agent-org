-- 0003_grants.sql — what the application role may do.
--
-- agent_org_app owns nothing, cannot create tables, and cannot bypass
-- row-level security. The audit log is append-only for it: INSERT only, no
-- UPDATE, no DELETE, no TRUNCATE. An outcome is a second INSERT referring
-- back to the intent row, never an edit of it.

-- The role is created without a password: `shannon migrate` sets it from
-- POSTGRES_APP_PASSWORD. A password has no business being in a migration.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_org_app') THEN
    CREATE ROLE agent_org_app LOGIN NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO agent_org_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    suppliers, components, products, boms, channels, tasks,
    action_proposals, approvals, agent_runs, order_history, reports
TO agent_org_app;

GRANT SELECT ON entities TO agent_org_app;
GRANT INSERT, SELECT ON audit_log TO agent_org_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM agent_org_app;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agent_org_app;
