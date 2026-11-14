-- Remove the external-source supplement added during career coverage investigation.
ALTER TABLE public.enterprise_coach_images DROP COLUMN IF EXISTS career_overrides;
