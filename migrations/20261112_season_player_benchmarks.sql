-- Apply after 20261112_report_percentiles.sql; shared numeric/tie helpers are reused.
BEGIN;
CREATE TABLE IF NOT EXISTS public.scoutwise_league_rank_meta_v1 (
 league_id bigint NOT NULL,season_id bigint NOT NULL,scope text NOT NULL,
 source_computed_at timestamptz NOT NULL,calculation_version integer NOT NULL,metric_catalog jsonb NOT NULL,
 PRIMARY KEY(league_id,season_id,scope)
);
CREATE TABLE IF NOT EXISTS public.scoutwise_league_rank_rows_v1 (
 league_id bigint NOT NULL,season_id bigint NOT NULL,scope text NOT NULL,player_id bigint NOT NULL,team_id bigint NOT NULL,
 minutes double precision NOT NULL,profile jsonb NOT NULL,metrics jsonb NOT NULL,
 PRIMARY KEY(league_id,season_id,scope,player_id,team_id),
 FOREIGN KEY(league_id,season_id,scope) REFERENCES public.scoutwise_league_rank_meta_v1 ON DELETE CASCADE
);
ALTER TABLE public.scoutwise_league_rank_meta_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scoutwise_league_rank_rows_v1 ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.scoutwise_league_rank_meta_v1,public.scoutwise_league_rank_rows_v1 FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.scoutwise_league_rank_meta_v1,public.scoutwise_league_rank_rows_v1 TO service_role,CURRENT_USER;

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

CREATE OR REPLACE FUNCTION public.scoutwise_league_rank_snapshot_changed_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP='TRUNCATE' THEN DELETE FROM public.scoutwise_league_rank_meta_v1;RETURN NULL;END IF;
 IF TG_OP='DELETE' THEN DELETE FROM public.scoutwise_league_rank_meta_v1 m WHERE m.league_id=OLD.league_id AND m.season_id=OLD.season_id AND m.scope=OLD.scope;RETURN OLD; END IF;
 IF TG_OP='UPDATE' AND (OLD.league_id,OLD.season_id,OLD.scope) IS DISTINCT FROM (NEW.league_id,NEW.season_id,NEW.scope) THEN
  DELETE FROM public.scoutwise_league_rank_meta_v1 m WHERE m.league_id=OLD.league_id AND m.season_id=OLD.season_id AND m.scope=OLD.scope;
 END IF;
 PERFORM public.scoutwise_store_league_rank_rows_v1(NEW.league_id,NEW.season_id,NEW.scope,NEW.source_computed_at,NEW.calculation_version,NEW.payload);
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS scoutwise_league_rank_snapshot_changed_v1 ON public.enterprise_league_player_rankings;
CREATE TRIGGER scoutwise_league_rank_snapshot_changed_v1 AFTER INSERT OR UPDATE OR DELETE ON public.enterprise_league_player_rankings
 FOR EACH ROW EXECUTE FUNCTION public.scoutwise_league_rank_snapshot_changed_v1();
DROP TRIGGER IF EXISTS scoutwise_league_rank_snapshot_truncated_v1 ON public.enterprise_league_player_rankings;
CREATE TRIGGER scoutwise_league_rank_snapshot_truncated_v1 AFTER TRUNCATE ON public.enterprise_league_player_rankings
 FOR EACH STATEMENT EXECUTE FUNCTION public.scoutwise_league_rank_snapshot_changed_v1();

CREATE OR REPLACE FUNCTION public.scoutwise_ensure_league_rank_rows_v1(l bigint,s bigint,sc text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,public AS $$
DECLARE published timestamptz;version integer;saved jsonb;
BEGIN
 SELECT p.source_computed_at,p.calculation_version INTO published,version FROM public.enterprise_league_player_rankings p WHERE p.league_id=l AND p.season_id=s AND p.scope=sc;
 IF NOT FOUND THEN RETURN false; END IF;
 IF EXISTS(SELECT 1 FROM public.scoutwise_league_rank_meta_v1 m WHERE m.league_id=l AND m.season_id=s AND m.scope=sc AND m.source_computed_at=published AND m.calculation_version=version) THEN RETURN true; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('scoutwise-league-rank-rows:'||l||':'||s||':'||sc,0));
 SELECT p.source_computed_at,p.calculation_version,p.payload INTO published,version,saved FROM public.enterprise_league_player_rankings p WHERE p.league_id=l AND p.season_id=s AND p.scope=sc FOR SHARE;
 IF NOT FOUND THEN RETURN false; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.scoutwise_league_rank_meta_v1 m WHERE m.league_id=l AND m.season_id=s AND m.scope=sc AND m.source_computed_at=published AND m.calculation_version=version) THEN
  PERFORM public.scoutwise_store_league_rank_rows_v1(l,s,sc,published,version,saved);
 END IF;
 RETURN true;
END $$;

CREATE OR REPLACE FUNCTION public.scoutwise_league_award_row_v1(profile jsonb,evidence jsonb,selected jsonb,mode text,player_position bigint,score float8)
RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path=pg_catalog,public AS $$
SELECT profile||jsonb_build_object('position',player_position,'awardMetric',selected->>'key',
 'awardValue',public.scoutwise_report_round_v1(score,4),'awardMode',CASE WHEN selected->>'key'='Minutes Played' THEN 'total' ELSE mode END,
 'awardUnit',selected->'unit','awardMatchesCovered',evidence->'matchesCovered','awardMinutesCovered',evidence->'minutesCovered')||
 CASE WHEN evidence ? 'denominator' THEN jsonb_build_object('awardNumerator',evidence->'numerator','awardDenominator',evidence->'denominator') ELSE '{}' END;
$$;

CREATE OR REPLACE FUNCTION public.scoutwise_league_player_page_v1(l bigint,s bigint,sc text,metric text,mode text,requested_direction text,page_offset integer,page_limit integer,period_key text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql STABLE PARALLEL SAFE SET search_path=pg_catalog,public AS $$
DECLARE catalog jsonb;selected jsonb;direction text;field text;published timestamptz;result jsonb;winner jsonb;
BEGIN
 SELECT m.metric_catalog,m.source_computed_at INTO catalog,published FROM public.scoutwise_league_rank_meta_v1 m WHERE m.league_id=l AND m.season_id=s AND m.scope=sc;
 IF NOT FOUND THEN RETURN jsonb_build_object('error','cache_not_ready'); END IF;
 SELECT COALESCE(jsonb_agg(m ORDER BY ord),'[]') INTO catalog FROM jsonb_array_elements(catalog) WITH ORDINALITY a(m,ord) WHERE m->>'key'<>'Match Count';
 SELECT m INTO selected FROM jsonb_array_elements(catalog) m WHERE m->>'key'=metric LIMIT 1;
 IF selected IS NULL THEN RETURN jsonb_build_object('error','unsupported_metric'); END IF;
 direction:=COALESCE(requested_direction,selected->>'direction');field:=CASE mode WHEN 'total' THEN 'value' WHEN 'perMatch' THEN 'perMatch' WHEN 'per90' THEN 'per90' END;
 WITH candidates AS MATERIALIZED (
  SELECT r.player_id,r.team_id,r.minutes,r.metrics->metric AS evidence,(r.metrics->metric->>field)::float8 AS score
  FROM public.scoutwise_league_rank_rows_v1 r WHERE r.league_id=l AND r.season_id=s AND r.scope=sc AND r.metrics->metric->>field IS NOT NULL
 ), ranked AS MATERIALIZED (
  SELECT c.*,rank() OVER(ORDER BY CASE WHEN direction='asc' THEN score END ASC,CASE WHEN direction<>'asc' THEN score END DESC) AS position,
   row_number() OVER(ORDER BY CASE WHEN direction='asc' THEN score END ASC,CASE WHEN direction<>'asc' THEN score END DESC,
    (evidence->>'matchesCovered')::float8 DESC,minutes DESC,player_id,team_id) AS ordinal
  FROM candidates c
 ), best AS MATERIALIZED (
  SELECT * FROM candidates c ORDER BY CASE WHEN selected->>'direction'='asc' THEN score END ASC,
   CASE WHEN selected->>'direction'<>'asc' THEN score END DESC,(evidence->>'matchesCovered')::float8 DESC,
   minutes DESC,player_id,team_id LIMIT 1
 ), chosen AS MATERIALIZED (
  SELECT * FROM ranked r WHERE r.ordinal>page_offset AND r.ordinal<=page_offset+page_limit
 ), formatted AS (
  SELECT c.ordinal,public.scoutwise_league_award_row_v1(r.profile,c.evidence,selected,mode,c.position,c.score) AS player
  FROM chosen c JOIN public.scoutwise_league_rank_rows_v1 r ON r.league_id=l AND r.season_id=s AND r.scope=sc AND r.player_id=c.player_id AND r.team_id=c.team_id
 )
 SELECT jsonb_build_object('metric_catalog',catalog,'metric',selected,'rows',COALESCE(jsonb_agg(f.player ORDER BY f.ordinal) FILTER(WHERE f.ordinal>page_offset AND f.ordinal<=page_offset+page_limit),'[]'),
  'total',(SELECT count(*) FROM candidates),'offset',page_offset,'limit',page_limit,'direction',direction,'mode',mode,
  'season_winner',(SELECT public.scoutwise_league_award_row_v1(r.profile,b.evidence,selected,mode,1,b.score) FROM best b JOIN public.scoutwise_league_rank_rows_v1 r ON r.league_id=l AND r.season_id=s AND r.scope=sc AND r.player_id=b.player_id AND r.team_id=b.team_id),'source_computed_at',published)
 INTO result FROM formatted f;
 SELECT p.payload->'period_awards'->period_key->metric->mode INTO winner FROM public.enterprise_league_player_rankings p WHERE p.league_id=l AND p.season_id=s AND p.scope=sc;
 RETURN result||jsonb_build_object('biweekly_winner',winner);
END $$;
REVOKE ALL ON FUNCTION public.scoutwise_store_league_rank_rows_v1(bigint,bigint,text,timestamptz,integer,jsonb),public.scoutwise_league_rank_snapshot_changed_v1(),public.scoutwise_ensure_league_rank_rows_v1(bigint,bigint,text),public.scoutwise_league_award_row_v1(jsonb,jsonb,jsonb,text,bigint,float8),public.scoutwise_league_player_page_v1(bigint,bigint,text,text,text,text,integer,integer,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.scoutwise_store_league_rank_rows_v1(bigint,bigint,text,timestamptz,integer,jsonb),public.scoutwise_ensure_league_rank_rows_v1(bigint,bigint,text),public.scoutwise_league_award_row_v1(jsonb,jsonb,jsonb,text,bigint,float8),public.scoutwise_league_player_page_v1(bigint,bigint,text,text,text,text,integer,integer,text) TO service_role,CURRENT_USER;
CREATE TABLE IF NOT EXISTS public.scoutwise_team_player_meta_v1 (
 league_id bigint NOT NULL,season_id bigint NOT NULL,scope text NOT NULL,published_at timestamptz NOT NULL,
 position_revision bigint NOT NULL,contribution_fingerprint text NOT NULL,profile_version integer NOT NULL,
 PRIMARY KEY(league_id,season_id,scope)
);
CREATE TABLE IF NOT EXISTS public.scoutwise_team_player_profiles_v1 (
 league_id bigint NOT NULL,season_id bigint NOT NULL,scope text NOT NULL,player_id bigint NOT NULL,
 profile jsonb NOT NULL,group_order text[] NOT NULL,PRIMARY KEY(league_id,season_id,scope,player_id),
 FOREIGN KEY(league_id,season_id,scope) REFERENCES public.scoutwise_team_player_meta_v1 ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS public.scoutwise_team_player_groups_v1 (
 league_id bigint NOT NULL,season_id bigint NOT NULL,scope text NOT NULL,gender text NOT NULL,group_key text NOT NULL,peer_count bigint NOT NULL,
 PRIMARY KEY(league_id,season_id,scope,gender,group_key),
 FOREIGN KEY(league_id,season_id,scope) REFERENCES public.scoutwise_team_player_meta_v1 ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS public.scoutwise_team_player_distributions_v1 (
 league_id bigint NOT NULL,season_id bigint NOT NULL,scope text NOT NULL,gender text NOT NULL,group_key text NOT NULL,label text NOT NULL,
 metric_values float8[] NOT NULL,PRIMARY KEY(league_id,season_id,scope,gender,group_key,label),
 FOREIGN KEY(league_id,season_id,scope) REFERENCES public.scoutwise_team_player_meta_v1 ON DELETE CASCADE
);
ALTER TABLE public.scoutwise_team_player_meta_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scoutwise_team_player_profiles_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scoutwise_team_player_groups_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scoutwise_team_player_distributions_v1 ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.scoutwise_team_player_meta_v1,public.scoutwise_team_player_profiles_v1,public.scoutwise_team_player_groups_v1,public.scoutwise_team_player_distributions_v1 FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.scoutwise_team_player_meta_v1,public.scoutwise_team_player_profiles_v1,public.scoutwise_team_player_groups_v1,public.scoutwise_team_player_distributions_v1 TO service_role,CURRENT_USER;

CREATE OR REPLACE FUNCTION public.scoutwise_build_team_player_distributions_v1(l bigint,s bigint,sc text)
RETURNS void LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,public AS $$
DECLARE cohort record;
BEGIN
 DELETE FROM public.scoutwise_team_player_groups_v1 g WHERE g.league_id=l AND g.season_id=s AND g.scope=sc;
 DELETE FROM public.scoutwise_team_player_distributions_v1 d WHERE d.league_id=l AND d.season_id=s AND d.scope=sc;
 FOR cohort IN SELECT p.profile->>'gender' AS gender,g.key AS group_key,count(*) AS n
  FROM public.scoutwise_team_player_profiles_v1 p CROSS JOIN LATERAL jsonb_object_keys(p.profile->'comparisonGroups') g(key)
  WHERE p.league_id=l AND p.season_id=s AND p.scope=sc AND (p.profile->>'minutes')::float8>=90
  GROUP BY p.profile->>'gender',g.key LOOP
  INSERT INTO public.scoutwise_team_player_groups_v1 VALUES(l,s,sc,cohort.gender,cohort.group_key,cohort.n);
  INSERT INTO public.scoutwise_team_player_distributions_v1
  SELECT l,s,sc,cohort.gender,cohort.group_key,m.key,
   array_agg(public.scoutwise_report_round_v1((m.value->>'value')::float8,6) ORDER BY public.scoutwise_report_round_v1((m.value->>'value')::float8,6))
  FROM public.scoutwise_team_player_profiles_v1 p CROSS JOIN LATERAL jsonb_each(p.profile->'metrics') m
  WHERE p.league_id=l AND p.season_id=s AND p.scope=sc AND p.profile->>'gender'=cohort.gender
   AND (p.profile->>'minutes')::float8>=90 AND p.profile->'comparisonGroups' ? cohort.group_key GROUP BY m.key;
 END LOOP;
END $$;

CREATE OR REPLACE FUNCTION public.scoutwise_team_player_percentiles_v1(l bigint,s bigint,sc text,team bigint,player bigint)
RETURNS jsonb LANGUAGE plpgsql STABLE PARALLEL SAFE SET search_path=pg_catalog,public AS $$
DECLARE source jsonb;group_names text[];selected_group text;source_gender text;cohort_size bigint;metric_row record;
 benchmark jsonb:='{}';metric_result jsonb;selected text:=NULL;a float8[];n integer;less integer;equal integer;
 v float8;score float8;player_position bigint;
BEGIN
 SELECT p.profile,p.group_order INTO source,group_names FROM public.scoutwise_team_player_profiles_v1 p
  WHERE p.league_id=l AND p.season_id=s AND p.scope=sc AND p.player_id=player;
 IF source IS NULL OR NOT (source->'teams' @> jsonb_build_array(team)) THEN
  RETURN jsonb_build_object('roles','{}'::jsonb,'benchmarks','{}'::jsonb,'selectedGroup',NULL,'metrics','{}'::jsonb);
 END IF;
 source_gender:=source->>'gender';
 FOREACH selected_group IN ARRAY group_names LOOP
  metric_result:='{}';
  SELECT g.peer_count INTO cohort_size FROM public.scoutwise_team_player_groups_v1 g
   WHERE g.league_id=l AND g.season_id=s AND g.scope=sc AND g.gender=source_gender AND g.group_key=selected_group;
  FOR metric_row IN SELECT m.key,m.value,d.metric_values FROM jsonb_each(source->'metrics') m
   LEFT JOIN public.scoutwise_team_player_distributions_v1 d ON d.league_id=l AND d.season_id=s AND d.scope=sc
    AND d.gender=source_gender AND d.group_key=selected_group AND d.label=m.key LOOP
   a:=metric_row.metric_values;n:=COALESCE(cardinality(a),0);
   IF (source->>'minutes')::float8<90 OR n<5 OR a[1]=a[n] THEN CONTINUE; END IF;
   v:=public.scoutwise_report_round_v1((metric_row.value->>'value')::float8,6);
   less:=public.scoutwise_report_bound_v1(a,v,false);equal:=public.scoutwise_report_bound_v1(a,v,true)-less;
   score:=100.0::float8*(2*less+equal)::float8/(2*n)::float8;
   IF (metric_row.value->>'lowerIsBetter')::boolean THEN score:=100-score;player_position:=1+less;
   ELSE player_position:=1+n-less-equal; END IF;
   metric_result:=jsonb_set(metric_result,ARRAY[metric_row.key],metric_row.value||jsonb_build_object(
    'season_value',metric_row.value->'value','percentile',public.scoutwise_report_round_v1(score,1),'peerCount',n,'position',player_position,'total_players',n));
  END LOOP;
  IF EXISTS(SELECT 1 FROM jsonb_each(metric_result) m WHERE (m.value->>'season_value')::float8<>0) THEN
   benchmark:=jsonb_set(benchmark,ARRAY[selected_group],source->'comparisonGroups'->selected_group||jsonb_build_object('peerCount',COALESCE(cohort_size,0),'metrics',metric_result));
   IF selected IS NULL THEN selected:=selected_group; END IF;
  END IF;
 END LOOP;
 RETURN jsonb_build_object('roles',source->'roles','benchmarks',benchmark,'selectedGroup',selected,'metrics',COALESCE(benchmark->selected->'metrics','{}'::jsonb));
END $$;
REVOKE ALL ON FUNCTION public.scoutwise_build_team_player_distributions_v1(bigint,bigint,text),public.scoutwise_team_player_percentiles_v1(bigint,bigint,text,bigint,bigint) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.scoutwise_build_team_player_distributions_v1(bigint,bigint,text),public.scoutwise_team_player_percentiles_v1(bigint,bigint,text,bigint,bigint) TO service_role,CURRENT_USER;
COMMIT;
