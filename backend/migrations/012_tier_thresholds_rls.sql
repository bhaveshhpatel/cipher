-- migration 012: RLS policies for tier_thresholds
-- Allows service-role key full access (Supabase default); authenticated users can SELECT.
-- Part of feature B-019: admin UI for tier threshold editing.

ALTER TABLE tier_thresholds ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all"     ON tier_thresholds;
DROP POLICY IF EXISTS "authenticated_select" ON tier_thresholds;

-- Authenticated users (admin UI) may read the active thresholds row
CREATE POLICY "authenticated_select"
  ON tier_thresholds
  FOR SELECT
  TO authenticated
  USING (true);

-- Keep updated_at trigger idempotent
CREATE OR REPLACE FUNCTION update_tier_thresholds_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_tier_thresholds_updated_at ON tier_thresholds;
CREATE TRIGGER trg_tier_thresholds_updated_at
  BEFORE UPDATE ON tier_thresholds
  FOR EACH ROW EXECUTE FUNCTION update_tier_thresholds_updated_at();
