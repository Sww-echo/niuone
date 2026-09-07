# Probe chase forward comparison

`probe-chase-forward-v1` is frozen before first sampling on September 8, 2026, under the strict-forward v51 protocol and source fingerprint. It measures the conditional effect of the first-Probe price-gain gate below 3%; actual orders, holdings, exits, and risk budgets retain their rules.

## Sampling and arms

- A sample is an emitted first-entry Probe BUY intent that passes every other execution gate. A new position after liquidation may qualify; adds and replacement intents do not.
- Activity, theme, setup, limit-up, sizing, cash, and capacity gates use the same actual account. Actual cumulative turnover of at least 3% is separate from the price-gain threshold.
- Baseline shadow-buys 100 shares for every qualifying intent. Filtered shadow-buys only when the actual quote is strictly below a 3% gain versus valid previous close; otherwise that opportunity holds cash with zero return.
- Keep the earliest qualifying intent per stock/day that reaches a complete durable decision payload. Retries, late records, and subsequent pullbacks cannot reassign its group. Failed, incomplete, and recent-state-only decisions do not qualify.

## Outcomes and missingness

Both arms close the observation at the fifth market session following entry. Fix slippage at 5 bp per side and freeze commission, minimum commission, transfer fee, and sell stamp duty through the production fee function. Returns include both-side costs. Normalize local adjusted daily bars using the prior market session's close relative to the frozen quote's previous close so a change in adjustment scale cannot create a return.

Use the existing exchange-calendar cache; without trusted coverage, conservatively require weekdays. Missing benchmark/stock bars, missing entry basis, or zero terminal volume remain pending or unverifiable. Never substitute the fifth available stock bar for the fifth market session, nor remove losing, suspended, or missing samples. Restored evidence may mature an outcome; a SQLite unique key makes completed outcomes append-only and immune to later refreshes.

The primary metric is the mean paired opportunity-return difference: filtered minus baseline, with skipped opportunities earning zero. This prevents a smaller trade denominator from masquerading as improvement. Secondary outputs include completed shadow-trade win rate, mean net return, sample counts, and `<3%`/`≥3%` price groups. Breakeven belongs in the denominator but is not a win; immature and unverifiable observations are reported separately.

Require at least 30 mature observations in each price group and three calendar months before manual review. The gate cannot establish efficacy or change production parameters automatically. Do not select thresholds, horizons, fees, or samples after observing results; a rule change requires a new version and cohort.

## Interpretation

This is a selected shadow event comparison. The model and actual portfolio already operate under current rules; candidates with no BUY intent are absent. Arms do not maintain independent portfolio and cash paths. Closing order books, queues, and executable price-limit conditions are not simulated. Results are not randomized evidence, an independent portfolio backtest, or live PnL. Review opportunity coverage, missingness, repeated-stock and entry-date/theme concentration before considering separate portfolio validation.

## Operations and audit

`practice_trader` records observations before account mutation. The existing 15:15 closing snapshot reads local daily bars and appends mature results to private SQLite `probe_chase_outcomes`. Strict-forward output includes protocol and aggregates under `probe_chase_comparison`; observations stay in private runtime storage.

Actual complete-trade win rate counts profitable, verified, fee-net zero-to-zero lifecycles over all completed lifecycles. Adds and partial exits belong to one trade attributed to the first entry. Profitable SELL-fill share is a separate exit-trigger metric. Missing durable history cannot be replaced by a truncated recent JSON history.

Decision-slot audits cover cohort start through evaluation day. They retain scheduler status, durable payload availability, candidate evidence, and controlled model-error types/HTTP codes. Successful empty-pool decisions qualify; intermediate system exits and failed retries do not. Failed history is preserved rather than rewritten as success.
