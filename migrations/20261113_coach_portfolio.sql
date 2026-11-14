CREATE TABLE IF NOT EXISTS public.enterprise_favorite_coaches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.enterprise_users(id) ON DELETE CASCADE,
    coach_id bigint NOT NULL CHECK (coach_id > 0),
    coach_payload jsonb NOT NULL CHECK (jsonb_typeof(coach_payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, coach_id)
);
ALTER TABLE public.enterprise_favorite_coaches ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.enterprise_favorite_coaches FROM anon, authenticated;
