CREATE TABLE IF NOT EXISTS public.enterprise_league_saved_views (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.enterprise_users(id) ON DELETE CASCADE,
    name text NOT NULL,
    settings jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS enterprise_league_saved_views_name
    ON public.enterprise_league_saved_views (user_id, (settings->>'tab'), lower(name));
ALTER TABLE public.enterprise_league_saved_views ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.enterprise_league_saved_views FROM anon, authenticated;
