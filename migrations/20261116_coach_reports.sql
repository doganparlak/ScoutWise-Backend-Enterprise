CREATE TABLE IF NOT EXISTS public.enterprise_coach_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.enterprise_users(id) ON DELETE CASCADE,
    coach_id bigint NOT NULL CHECK (coach_id > 0),
    selection_key text NOT NULL,
    language text NOT NULL CHECK (language IN ('tr', 'en')),
    version integer NOT NULL DEFAULT 1,
    coach_payload jsonb NOT NULL,
    history_payload jsonb NOT NULL,
    period_ids jsonb NOT NULL,
    report_status text NOT NULL DEFAULT 'processing' CHECK (report_status IN ('processing', 'ready', 'failed')),
    report_content jsonb NOT NULL DEFAULT '{}'::jsonb,
    report_error text,
    report_ready_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, coach_id, selection_key, language, version)
);
CREATE INDEX IF NOT EXISTS enterprise_coach_reports_user_coach_idx
    ON public.enterprise_coach_reports(user_id, coach_id, created_at DESC);
ALTER TABLE public.enterprise_coach_reports ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.enterprise_coach_reports FROM anon, authenticated;
COMMENT ON TABLE public.enterprise_coach_reports IS
    'User-owned coach reports with immutable career/selection snapshots and persisted interactive section results, independent of portfolio membership.';
