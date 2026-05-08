-- 014_admin_activity_log.sql  [ADMIN-UI-001 / STORY-BE-001]
-- Append-only audit log for every mutating admin action.
-- Written exclusively via service role; surfaced via GET /api/admin/activity-log.
--
-- Promoted from backend/migrations/016_admin_activity_log.sql to the canonical
-- Supabase migration sequence on 2026-05-08 to resolve PGRST205 (table not in
-- schema cache) causing 500 on GET /api/admin/activity-log in production.

CREATE TABLE IF NOT EXISTS admin_activity_log (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    admin_email text        NOT NULL,
    action      text        NOT NULL,
    detail      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    ip_address  text
);

CREATE INDEX IF NOT EXISTS admin_activity_log_created_at_idx
    ON admin_activity_log (created_at DESC);

CREATE INDEX IF NOT EXISTS admin_activity_log_action_idx
    ON admin_activity_log (action);

CREATE INDEX IF NOT EXISTS admin_activity_log_admin_email_idx
    ON admin_activity_log (admin_email);

-- Block anon / authenticated roles; service_role bypasses RLS automatically.
ALTER TABLE admin_activity_log ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE admin_activity_log IS
  'Append-only log of admin mutations. Written by service role; read via GET /api/admin/activity-log.';
