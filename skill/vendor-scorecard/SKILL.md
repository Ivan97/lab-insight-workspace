---
name: vendor-scorecard
description: Use when a question compares vendors, or asks which vendor is best, worst, or should be reviewed. Defines the house rules for ranking vendors so every scorecard is built the same way.
---

# Vendor scorecard

When ranking vendors, always report these four columns together, in this order:
`test_count`, `total_cost_usd`, `pass_rate_pct`, `avg_turnaround_days`.

House rules:

- A vendor with fewer than 20 tests is still shown, but flagged as low sample.
- Never call a vendor "best" on cost alone. Say which dimension the claim rests on.
- When a vendor sits below its contracted quality target, say so explicitly.
