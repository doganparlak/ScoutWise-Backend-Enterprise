-- Run in autocommit mode. These concurrent indexes cover the three existing name predicates.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
SET search_path TO pg_catalog, public, extensions;

CREATE INDEX CONCURRENTLY IF NOT EXISTS player_data_name_trgm_idx
    ON public.player_data USING gin ((metadata->>'player_name') gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS player_data_normalized_name_trgm_idx
    ON public.player_data USING gin ((metadata->>'player_name_norm') gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS player_data_folded_name_trgm_idx
    ON public.player_data USING gin ((LOWER(TRANSLATE(COALESCE(metadata->>'player_name', ''), 'çğıöşüÇĞİÖŞÜIİıáàâäãåāăąÁÀÂÄÃÅĀĂĄéèêëēĕėęěÉÈÊËĒĔĖĘĚíìîïīĭįİÍÌÎÏĪĬĮóòôöõøōŏőÓÒÔÖÕØŌŎŐúùûüūŭůűųÚÙÛÜŪŬŮŰŲñÑćčĆČłŁńŃřŘśšŚŠýÿÝŸžźżŽŹŻ', 'cgiosuCGIOSUiiiaaaaaaaaaAAAAAAAAAeeeeeeeeeEEEEEEEEEiiiiiiiiIIIIIIIoooooooooOOOOOOOOOuuuuuuuuuUUUUUUUUUnNccCClLnNrRssSSyyYYzzzZZZ'))) gin_trgm_ops);

RESET search_path;
