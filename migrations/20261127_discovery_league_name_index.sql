-- Run in autocommit mode. Keep this expression aligned with league_name_sql.
CREATE INDEX CONCURRENTLY IF NOT EXISTS player_data_discovery_league_name_idx
    ON public.player_data (
        REGEXP_REPLACE(
            TRIM(LOWER(TRANSLATE(
                COALESCE(metadata->>'league_name', ''),
                'çğıöşüÇĞİÖŞÜIİıáàâäãåāăąÁÀÂÄÃÅĀĂĄéèêëēĕėęěÉÈÊËĒĔĖĘĚíìîïīĭįİÍÌÎÏĪĬĮóòôöõøōŏőÓÒÔÖÕØŌŎŐúùûüūŭůűųÚÙÛÜŪŬŮŰŲñÑćčĆČłŁńŃřŘśšŚŠýÿÝŸžźżŽŹŻ',
                'cgiosuCGIOSUiiiaaaaaaaaaAAAAAAAAAeeeeeeeeeEEEEEEEEEiiiiiiiiIIIIIIIoooooooooOOOOOOOOOuuuuuuuuuUUUUUUUUUnNccCClLnNrRssSSyyYYzzzZZZ'
            ))),
            '[[:space:]]+', ' ', 'g'
        )
    );
