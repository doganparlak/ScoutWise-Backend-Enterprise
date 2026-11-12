# Pre-match selected-player ranks

Apply `20261112_report_percentiles.sql`, `20261112_season_player_benchmarks.sql`, then
`20261112_pre_match_player_ranks.sql` before deploying the enterprise backend.
The migrations have been applied to the connected Supabase project. Deployment of
Python changes is separate.

Pre-match comparisons and the team portfolio's raw player ranks request only the
players they display. No full-league Python ranking map or process LRU is retained.
Current lookups reuse the normalized published leaderboard. Sorted metric values
are stored once per league/season/scope/mode; strict competition ranks use binary
search and preserve ties, ascending metrics, eligibility, raw-score comparison,
and four-decimal display rounding.

A published snapshot newer than the report cutoff is never used. Historical misses
stream saved contributions strictly before the cutoff, preserving European scope
and version checks. A source-metadata fingerprint detects corrections. The first
request builds a shared, mode-independent historical snapshot under a cohort lock;
subsequent requests return selected players only. Historical snapshots are retained
for the 24 most recently used keys, with in-flight keys protected from eviction.
Cleanup is performed during historical requests; retained snapshots persist until
the next such request. Source contributions and saved reports are untouched.

The shared benchmark connection pool permits at most four connections per process.
Cold builds still need a temporary aggregation working set; different historical
cutoffs can build concurrently within that bound. This is not a claim that all
backend memory spikes are eliminated. Snapshot publication also refreshes the SQL
value distributions. RLS and function ACLs restrict access to backend roles.

Verification (local client against Supabase, November 12, 2026):
- 10,103 metric results identical to the previous implementation across two Super
  Lig seasons and Champions League overall/qualifying, present/historical cutoffs,
  and total/per-match/per-90 modes.
- Full-season, 24 selected players: extra peak process memory 27.07 to 0.56 MiB
  for one request; 74.35 to 2.51 MiB for four simultaneous requests.
- Four requests for one cold historical key: 25.68 MiB / 9.12 seconds total;
  warm repeat: 2.39 MiB / 3.67 seconds total, including client/network round trips.
- Warm selected-rank SQL: 70.018 ms for 24 players; cache checks 0.133/0.099 ms;
  all three plans show zero temporary reads/writes and zero WAL bytes.
- Sorted arrays occupied 2.02 MiB across the tested published/historical cohorts.

Measurements describe incremental client-process memory, not total Render RSS.

Bounded Supabase load check: 40/40 successful requests (24 with four users, 16 with
eight). Median latency 0.68/0.91 seconds; maximum 1.84 seconds. CPU was about 1.03%
in the baseline interval and 6.95% in the sampled interval covering load. Metrics
are sampled roughly once per minute, so these are interval averages, not CPU peaks.
Available RAM stayed above 773.75 MiB, and existing swap use decreased from 260.55
to 260.49 MiB. No deadlocks, query failures, or derived-cache growth occurred.
The global database temporary-write counter increased by 28.94 MiB during the
broader window; it includes unrelated traffic. The tested feature's own warm query
plans had zero temporary writes. This is evidence for keeping Small for the tested
load, not a guarantee about every production workload.

Additional rollback-only checks passed: raw-value ties below display precision,
ascending/descending and signed/zero values, absent players, future snapshot
rejection, refresh invalidation, bounded historical retention, and client-role
access denial. No synthetic data was retained.
