-- Run in autocommit mode. PostgreSQL cannot build a concurrent index inside a transaction block.
-- The expression must match _folded_column_sql('player_name') in season_data.py.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
SET search_path TO pg_catalog, public, extensions;

CREATE INDEX CONCURRENTLY IF NOT EXISTS player_comp_season_data_folded_name_trgm_idx
    ON public.player_comp_season_data USING gin (
        (BTRIM(LOWER(REGEXP_REPLACE(TRANSLATE(COALESCE(player_name, ''), 'çğıöşüÇĞİÖŞÜIİıáàâäãåāăąÁÀÂÄÃÅĀĂĄéèêëēĕėęěÉÈÊËĒĔĖĘĚíìîïīĭįİÍÌÎÏĪĬĮóòôöõøōŏőÓÒÔÖÕØŌŎŐúùûüūŭůűųÚÙÛÜŪŬŮŰŲñÑćčĆČłŁńŃřŘśšŚŠýÿÝŸžźżŽŹŻ', 'cgiosuCGIOSUiiiaaaaaaaaaAAAAAAAAAeeeeeeeeeEEEEEEEEEiiiiiiiiIIIIIIIoooooooooOOOOOOOOOuuuuuuuuuUUUUUUUUUnNccCClLnNrRssSSyyYYzzzZZZ'), '[^a-zA-Z0-9]+', ' ', 'g')))) gin_trgm_ops
    );

RESET search_path;
