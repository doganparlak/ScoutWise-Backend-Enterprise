# Season player benchmarks and leaderboard pages

Apply `20261112_report_percentiles.sql` first, then
`20261112_season_player_benchmarks.sql`, before deploying this backend.
Both migrations are idempotent. They add only derived caches/functions/triggers;
source player statistics, fixture contributions, and saved reports are unchanged.

## Team-report player percentiles

Season values still come from the same published fixture contributions, with
90-minute minimums, identical per-stat coverage, rate evidence, role eligibility,
gender separation, and tied midranks. Players' club stints are pooled exactly as
before; the requested team remains a membership check, not a new metric scope.

The existing aggregation runs once when a season cache is missing/outdated.
Fixture rows stream in batches of 20, finalized profiles are written in batches
of 64, and accumulation objects are released during finalization. A per-cohort
transaction lock prevents parallel copies of this rebuild. No whole-league LRU
profile maps remain in the API process. A cold rebuild still has a bounded
backend working set; ordinary requests retrieve only the requested player.

Freshness keys include publication time, source-contribution metadata fingerprint,
position-data revision, and profile algorithm version. Metadata checks do not
read fixture player-stat JSON. Unpublished/incompatible source changes return the
same pending state. Sorted distributions are persisted in Supabase and the shared
binary-search/rounding helpers perform ranking. Bump the profile cache version
when aggregation rules change independently of source publications.

## League Analysis Center player rankings

Published leaderboard JSON is normalized into one compact database row per
player/team. A source-snapshot trigger refreshes it atomically on insert/update
and clears it on delete/truncate. Existing published snapshots materialize once
on first access under a per-snapshot lock. Ordinary API requests read metadata,
the requested page, and winners only. Ranking, sort order, ties, eligibility,
metric catalogue, pagination, image hydration, and period winners are preserved.
Season winners retain the metric's default direction even if the user reverses
the visible table sort. No schema DDL occurs in the read endpoint.

The existing streamed, serialized compatibility rebuild for missing/old source
leaderboards is preserved. Normalized page construction itself occurs inside
Supabase, and future publisher snapshots maintain it automatically.

## Operations

Derived caches are keyed by season/scope/player, not by users or requests.
Rebuilding replaces a cohort rather than appending per-request copies. RLS is
enabled and client/public roles have no direct access. The bounded benchmark
connection pool keeps cache transactions independent of caller transactions.
The Data Module and league publication workflows require no manual cache step.
Team rankings and legacy pre-match player-rank maps are separate code paths.
