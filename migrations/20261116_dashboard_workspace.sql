CREATE TABLE IF NOT EXISTS public.enterprise_dashboard_workspace (
    user_id uuid PRIMARY KEY REFERENCES public.enterprise_users(id) ON DELETE CASCADE,
    pinned_reports jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(pinned_reports)='array'),
    recent_reports jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(recent_reports)='array'),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.enterprise_dashboard_workspace ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.enterprise_dashboard_workspace FROM anon, authenticated;
COMMENT ON TABLE public.enterprise_dashboard_workspace IS
    'Account-owned report pins and recent report opens. Access only through authenticated owner-scoped Enterprise API.';
