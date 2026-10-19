# GC/XAU Order-Flow Research Pipeline

Purpose: ingest CME GC trades (Databento) + broker XAUUSD ticks (MT5), build session/volume-profile
features, run pre-registered event studies, backtest, validate. Python 3.11, pandas, pyarrow, duckdb,
numpy, numba, scipy, statsmodels, scikit-learn, arch, pytest, ruff.

## Hard rules
1. Raw data in data/raw* is IMMUTABLE. Never edit, overwrite or delete it. Derived data must be rebuildable from raw.
2. All timestamps stored UTC (ns). Session logic uses America/New_York wall-clock via src/config.py ONLY.
   Never hardcode session boundaries elsewhere.
3. NO LOOK-AHEAD. A session's profile/levels may only be used by sessions that start after it ends.
   Features are computed from completed data only.
4. NETWORK/SPEND: before ANY Databento pull, print metadata.get_cost() and STOP for my approval. Never pull
   more than the approved range. Never read, print or commit .env or API keys; use environment variables.
5. Databento `side` semantics, contract-roll handling and the MT5 server-time offset are uncertain: if the
   docs/data don't settle it, STOP and ask. Do not guess.

## Research integrity
- NEVER modify docs/prereg.md, docs/acceptance.yaml or docs/SPEC.md. Propose changes in docs/PROPOSALS.md.
- NEVER tune any parameter on out-of-sample data. No parameter search unless the task says so.
- Every statistical test calls trials.log(name) (src/trials.py). The trial count feeds the Deflated Sharpe.
- Report negative and null results as plainly as positive ones.
- Do not weaken, skip or delete a failing test to make it pass. If a test seems wrong, explain why and ask.

## Engineering
- Type hints, docstrings on public functions, deterministic seeds (default 0), numba for hot loops.
- Pipeline code in src/, never in notebooks. SQL via DuckDB over Parquet (hive partitioning).
- `make check` (ruff + pytest) must pass before you say a phase is done.
- Small, atomic commits. Update docs/PROGRESS.md at the end of every phase: what was built, assumptions made,
  open questions.