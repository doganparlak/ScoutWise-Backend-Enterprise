-- Run in autocommit mode for the concurrent index build.
-- Keep the role aliases here aligned with role_short_sql and role_value_short_sql.
CREATE OR REPLACE FUNCTION public.scoutwise_similarity_candidate_roles(metadata jsonb)
RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT ARRAY[CASE
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) IN ('g', 'gk', 'goalkeeper', 'goal keeper') THEN 'GK'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'left wing back' THEN 'LWB'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'left back' THEN 'LB'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'left center back' THEN 'LCB'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) IN ('center back', 'centre back') THEN 'CB'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'right center back' THEN 'RCB'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'right back' THEN 'RB'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'right wing back' THEN 'RWB'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'left midfield' THEN 'LM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'left defensive midfield' THEN 'LDM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'left center midfield' THEN 'LCM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'left attacking midfield' THEN 'LAM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) IN ('center midfield', 'central midfield') THEN 'CM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) IN ('center attacking midfield', 'attacking midfield') THEN 'CAM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) IN ('center defensive midfield', 'defensive midfield') THEN 'CDM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'right defensive midfield' THEN 'RDM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'right center midfield' THEN 'RCM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'right attacking midfield' THEN 'RAM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'right midfield' THEN 'RM'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) IN ('a', 'f', 'center forward', 'centre forward', 'attacker', 'forward') THEN 'CF'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'right center forward' THEN 'RCF'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'left center forward' THEN 'LCF'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'left wing' THEN 'LW'
        WHEN LOWER(COALESCE(metadata->>'position_name', '')) = 'right wing' THEN 'RW'
        ELSE metadata->>'position_name'
    END]
        || ARRAY(
            SELECT CASE
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) IN ('g', 'gk', 'goalkeeper', 'goal keeper') THEN 'GK'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'lwb' THEN 'LWB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'left wing back' THEN 'LWB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'lb' THEN 'LB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'left back' THEN 'LB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'lcb' THEN 'LCB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'left center back' THEN 'LCB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) IN ('cb', 'center back', 'centre back') THEN 'CB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'rcb' THEN 'RCB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'right center back' THEN 'RCB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'rb' THEN 'RB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'right back' THEN 'RB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'rwb' THEN 'RWB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'right wing back' THEN 'RWB'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'lm' THEN 'LM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'left midfield' THEN 'LM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'ldm' THEN 'LDM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'left defensive midfield' THEN 'LDM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'lcm' THEN 'LCM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'left center midfield' THEN 'LCM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'lam' THEN 'LAM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'left attacking midfield' THEN 'LAM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) IN ('cm', 'center midfield', 'central midfield') THEN 'CM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) IN ('cam', 'center attacking midfield', 'attacking midfield') THEN 'CAM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) IN ('cdm', 'center defensive midfield', 'defensive midfield') THEN 'CDM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'rdm' THEN 'RDM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'right defensive midfield' THEN 'RDM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'rcm' THEN 'RCM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'right center midfield' THEN 'RCM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'ram' THEN 'RAM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'right attacking midfield' THEN 'RAM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'rm' THEN 'RM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'right midfield' THEN 'RM'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) IN ('a', 'f', 'cf', 'center forward', 'centre forward', 'attacker', 'forward') THEN 'CF'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'rcf' THEN 'RCF'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'right center forward' THEN 'RCF'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'lcf' THEN 'LCF'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'left center forward' THEN 'LCF'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'lw' THEN 'LW'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'left wing' THEN 'LW'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'rw' THEN 'RW'
        WHEN LOWER(TRIM(COALESCE(discovery_role.value, ''))) = 'right wing' THEN 'RW'
        ELSE UPPER(TRIM(COALESCE(discovery_role.value, '')))
    END
            FROM jsonb_object_keys(
                CASE WHEN jsonb_typeof(metadata->'position_counts') = 'object'
                    THEN metadata->'position_counts' ELSE '{}'::jsonb END
            ) AS discovery_role(value)
        )
$$;

CREATE INDEX CONCURRENTLY IF NOT EXISTS player_data_similarity_roles_gin_idx
    ON public.player_data USING gin (public.scoutwise_similarity_candidate_roles(metadata));

ANALYZE public.player_data;
