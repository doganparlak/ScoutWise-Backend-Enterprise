-- Cache career evidence from Sportmonks separately from appointment profiles.
ALTER TABLE public.enterprise_coach_images
    ADD COLUMN IF NOT EXISTS career_data jsonb,
    ADD COLUMN IF NOT EXISTS career_fetched_at timestamptz;
