-- Coach identity/image directory. Refreshed explicitly by coach_pool_module.sync.
CREATE TABLE IF NOT EXISTS public.enterprise_coach_images (
    coach_id bigint PRIMARY KEY CHECK (coach_id > 0),
    coach_name text NOT NULL,
    image_url text,
    image_status text NOT NULL DEFAULT 'pending' CHECK (image_status IN ('pending', 'available', 'missing', 'failed')),
    source text NOT NULL DEFAULT 'sportmonks',
    last_fetched_at timestamptz,
    last_verified_at timestamptz,
    fetch_attempt_count integer NOT NULL DEFAULT 0 CHECK (fetch_attempt_count >= 0),
    last_fetch_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    search_text text NOT NULL DEFAULT '',
    profile_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT available_coach_image_requires_url CHECK (image_status <> 'available' OR NULLIF(btrim(image_url), '') IS NOT NULL)
);
COMMENT ON TABLE public.enterprise_coach_images IS 'Sportmonks coach names, image paths and profile snapshots; web searches read this directory without upstream calls.';
ALTER TABLE public.enterprise_coach_images ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.enterprise_coach_images FROM anon, authenticated;
