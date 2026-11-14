CREATE TABLE IF NOT EXISTS public.enterprise_coach_comparison_cache (
    cache_key text PRIMARY KEY,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    expires_at timestamptz NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.enterprise_coach_comparison_cache IS 'Shared slim Sportmonks team-statistic fixtures and coach comparison aggregates; no lineups or user selections.';
ALTER TABLE public.enterprise_coach_comparison_cache ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.enterprise_coach_comparison_cache FROM anon, authenticated;
