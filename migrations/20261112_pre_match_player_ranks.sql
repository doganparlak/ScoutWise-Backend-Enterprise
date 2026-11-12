-- Apply after the scouting and season benchmark migrations.
BEGIN;
ALTER TABLE public.scoutwise_league_rank_meta_v1 ADD COLUMN IF NOT EXISTS rank_distribution_version integer NOT NULL DEFAULT 0;
ALTER TABLE public.scoutwise_league_rank_meta_v1 ADD COLUMN IF NOT EXISTS last_used_at timestamptz NOT NULL DEFAULT NOW();
CREATE TABLE IF NOT EXISTS public.scoutwise_league_rank_distributions_v1 (
 league_id bigint NOT NULL,season_id bigint NOT NULL,scope text NOT NULL,label text NOT NULL,mode text NOT NULL,
 metric_values double precision[] NOT NULL,PRIMARY KEY(league_id,season_id,scope,label,mode),
 FOREIGN KEY(league_id,season_id,scope) REFERENCES public.scoutwise_league_rank_meta_v1 ON DELETE CASCADE
);
ALTER TABLE public.scoutwise_league_rank_distributions_v1 ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.scoutwise_league_rank_distributions_v1 FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.scoutwise_league_rank_distributions_v1 TO service_role,CURRENT_USER;

CREATE OR REPLACE FUNCTION public.scoutwise_build_league_rank_distributions_v1(l bigint,s bigint,sc text)
RETURNS void LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,public AS $$
DECLARE metric record;basis record;a float8[];
BEGIN
 DELETE FROM public.scoutwise_league_rank_distributions_v1 d WHERE d.league_id=l AND d.season_id=s AND d.scope=sc;
 FOR metric IN SELECT m->>'key' AS label FROM public.scoutwise_league_rank_meta_v1 meta
  CROSS JOIN LATERAL jsonb_array_elements(meta.metric_catalog) m WHERE meta.league_id=l AND meta.season_id=s AND meta.scope=sc LOOP
  FOR basis IN SELECT * FROM (VALUES('total','value'),('perMatch','perMatch'),('per90','per90')) b(mode,field) LOOP
   SELECT array_agg((r.metrics->metric.label->>basis.field)::float8 ORDER BY (r.metrics->metric.label->>basis.field)::float8)
    INTO a FROM public.scoutwise_league_rank_rows_v1 r WHERE r.league_id=l AND r.season_id=s AND r.scope=sc
    AND r.metrics->metric.label->>basis.field IS NOT NULL;
   IF a IS NOT NULL THEN INSERT INTO public.scoutwise_league_rank_distributions_v1 VALUES(l,s,sc,metric.label,basis.mode,a); END IF;
  END LOOP;
 END LOOP;
 UPDATE public.scoutwise_league_rank_meta_v1 m SET rank_distribution_version=1 WHERE m.league_id=l AND m.season_id=s AND m.scope=sc;
END $$;

CREATE OR REPLACE FUNCTION public.scoutwise_ensure_league_rank_distributions_v1(l bigint,s bigint,sc text)
RETURNS void LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,public AS $$
DECLARE version integer;
BEGIN
 SELECT m.rank_distribution_version INTO version FROM public.scoutwise_league_rank_meta_v1 m WHERE m.league_id=l AND m.season_id=s AND m.scope=sc;
 IF version=1 THEN RETURN; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('scoutwise-rank-distributions:'||l||':'||s||':'||sc,0));
 SELECT m.rank_distribution_version INTO version FROM public.scoutwise_league_rank_meta_v1 m WHERE m.league_id=l AND m.season_id=s AND m.scope=sc FOR UPDATE;
 IF NOT FOUND OR version=1 THEN RETURN; END IF;
 PERFORM public.scoutwise_build_league_rank_distributions_v1(l,s,sc);
END $$;

CREATE OR REPLACE FUNCTION public.scoutwise_selected_player_ranks_v1(l bigint,s bigint,sc text,targets jsonb,requested_mode text,cutoff timestamptz)
RETURNS jsonb LANGUAGE plpgsql STABLE PARALLEL SAFE SET search_path=pg_catalog,public AS $$
DECLARE catalogue jsonb;player record;metric record;field text;score float8;a float8[];n integer;position_value bigint;
 lower_value boolean;basis text;metrics jsonb;result jsonb:='[]';kind jsonb;
BEGIN
 SELECT jsonb_object_agg(m->>'key',jsonb_build_object('aggregation',m->>'aggregation','direction',m->>'direction'))
 INTO catalogue FROM public.scoutwise_league_rank_meta_v1 meta CROSS JOIN LATERAL jsonb_array_elements(meta.metric_catalog) m
 WHERE meta.league_id=l AND meta.season_id=s AND meta.scope=sc AND meta.source_computed_at<=cutoff AND meta.rank_distribution_version=1;
 IF catalogue IS NULL THEN RETURN NULL; END IF;
 field:=CASE requested_mode WHEN 'total' THEN 'value' WHEN 'perMatch' THEN 'perMatch' WHEN 'per90' THEN 'per90' END;
 FOR player IN SELECT r.player_id,r.team_id,r.metrics FROM public.scoutwise_league_rank_rows_v1 r
  WHERE r.league_id=l AND r.season_id=s AND r.scope=sc AND EXISTS(SELECT 1 FROM jsonb_array_elements(targets) t
    WHERE (t->>0)::bigint=r.team_id AND (t->>1)::bigint=r.player_id) ORDER BY r.team_id,r.player_id LOOP
  metrics:='{}';
  FOR metric IN SELECT m.key,m.value,d.metric_values FROM jsonb_each(player.metrics) m
   LEFT JOIN public.scoutwise_league_rank_distributions_v1 d ON d.league_id=l AND d.season_id=s AND d.scope=sc AND d.label=m.key AND d.mode=requested_mode LOOP
   score:=public.scoutwise_report_number_v1(metric.value->field,true);
   IF score IS NULL THEN CONTINUE; END IF;
   a:=metric.metric_values;n:=COALESCE(cardinality(a),0);
   IF n=0 THEN CONTINUE; END IF;
   kind:=catalogue->metric.key;lower_value:=COALESCE(kind->>'direction'='asc',false);
   position_value:=1+CASE WHEN lower_value THEN public.scoutwise_report_bound_v1(a,score,false)
     ELSE n-public.scoutwise_report_bound_v1(a,score,true) END;
   basis:=CASE WHEN metric.key='Rating' THEN 'average_rating' WHEN kind->>'aggregation'='derived_rate' THEN 'rate'
      WHEN kind->>'aggregation'='average' THEN 'average' ELSE requested_mode END;
   metrics:=jsonb_set(metrics,ARRAY[metric.key],jsonb_build_object('position',position_value,'total_players',n,
    'season_value',public.scoutwise_report_round_v1(score,4),'basis',basis,'direction',CASE WHEN lower_value THEN 'asc' ELSE 'desc' END));
  END LOOP;
  IF metrics<>'{}' THEN result:=result||jsonb_build_array(jsonb_build_object('team_id',player.team_id,'player_id',player.player_id,'metrics',metrics)); END IF;
 END LOOP;
 RETURN result;
END $$;

CREATE OR REPLACE FUNCTION public.scoutwise_trim_pre_match_histories_v1(keep integer DEFAULT 24)
RETURNS void LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,public AS $$
DECLARE old record;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('scoutwise-pre-match-history-retention',0));
 FOR old IN SELECT m.league_id,m.season_id,m.scope FROM public.scoutwise_league_rank_meta_v1 m
  WHERE m.scope LIKE 'pre_match:%' ORDER BY m.last_used_at DESC,m.league_id,m.season_id,m.scope OFFSET GREATEST(1,keep) LOOP
  IF pg_try_advisory_xact_lock(hashtextextended('scoutwise-pre-match-history:'||old.league_id||':'||old.season_id||':'||old.scope,0)) THEN
   DELETE FROM public.scoutwise_league_rank_meta_v1 m WHERE m.league_id=old.league_id AND m.season_id=old.season_id AND m.scope=old.scope;
  END IF;
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION public.scoutwise_store_league_rank_rows_v1(l bigint,s bigint,sc text,published timestamptz,version integer,data jsonb)
RETURNS void LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,public AS $$
BEGIN
 INSERT INTO public.scoutwise_league_rank_meta_v1(league_id,season_id,scope,source_computed_at,calculation_version,metric_catalog) VALUES(l,s,sc,published,version,COALESCE(data->'metric_catalog','[]'))
 ON CONFLICT(league_id,season_id,scope) DO UPDATE SET source_computed_at=EXCLUDED.source_computed_at,
 calculation_version=EXCLUDED.calculation_version,metric_catalog=EXCLUDED.metric_catalog;
 DELETE FROM public.scoutwise_league_rank_rows_v1 r WHERE r.league_id=l AND r.season_id=s AND r.scope=sc;
 INSERT INTO public.scoutwise_league_rank_rows_v1
 SELECT l,s,sc,(p->>'playerId')::bigint,(p->>'teamId')::bigint,(p->>'minutes')::float8,p-'metrics',p->'metrics'
 FROM jsonb_array_elements(data->'players') p;
 IF to_regprocedure('public.scoutwise_build_league_rank_distributions_v1(bigint,bigint,text)') IS NOT NULL THEN
  UPDATE public.scoutwise_league_rank_meta_v1 m SET rank_distribution_version=0 WHERE m.league_id=l AND m.season_id=s AND m.scope=sc;
  PERFORM public.scoutwise_build_league_rank_distributions_v1(l,s,sc);
 END IF;
END $$;
REVOKE ALL ON FUNCTION public.scoutwise_build_league_rank_distributions_v1(bigint,bigint,text),public.scoutwise_ensure_league_rank_distributions_v1(bigint,bigint,text),public.scoutwise_selected_player_ranks_v1(bigint,bigint,text,jsonb,text,timestamptz),public.scoutwise_trim_pre_match_histories_v1(integer) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.scoutwise_build_league_rank_distributions_v1(bigint,bigint,text),public.scoutwise_ensure_league_rank_distributions_v1(bigint,bigint,text),public.scoutwise_selected_player_ranks_v1(bigint,bigint,text,jsonb,text,timestamptz),public.scoutwise_trim_pre_match_histories_v1(integer) TO service_role,CURRENT_USER;
COMMIT;
