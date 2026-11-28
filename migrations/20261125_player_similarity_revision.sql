BEGIN;

CREATE TABLE IF NOT EXISTS public.scoutwise_player_data_revision (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL DEFAULT 0
);
ALTER TABLE public.scoutwise_player_data_revision ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.scoutwise_player_data_revision FROM PUBLIC;
INSERT INTO public.scoutwise_player_data_revision (singleton) VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

CREATE OR REPLACE FUNCTION public.scoutwise_bump_player_data_revision()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    UPDATE public.scoutwise_player_data_revision
    SET revision = revision + 1
    WHERE singleton;
    RETURN NULL;
END;
$$;

REVOKE ALL ON FUNCTION public.scoutwise_bump_player_data_revision() FROM PUBLIC;
DROP TRIGGER IF EXISTS scoutwise_player_data_revision_changed ON public.player_data;
CREATE TRIGGER scoutwise_player_data_revision_changed
AFTER TRUNCATE OR INSERT OR UPDATE OR DELETE ON public.player_data
FOR EACH STATEMENT EXECUTE FUNCTION public.scoutwise_bump_player_data_revision();

COMMIT;
