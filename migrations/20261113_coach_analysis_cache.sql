CREATE TABLE IF NOT EXISTS public.enterprise_coach_analysis_cache (
    coach_id bigint PRIMARY KEY REFERENCES public.enterprise_coach_images(coach_id) ON DELETE CASCADE,
    schema_version integer NOT NULL,
    source_fingerprint text NOT NULL,
    through_date date NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    generated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
COMMENT ON TABLE public.enterprise_coach_analysis_cache IS
    'Shared Sportmonks career and match-eligibility summaries; excludes raw match statistics and user selections.';
ALTER TABLE public.enterprise_coach_analysis_cache ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.enterprise_coach_analysis_cache FROM anon, authenticated;
