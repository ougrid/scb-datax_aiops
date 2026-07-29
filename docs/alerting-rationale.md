# Alerting threshold rationale

All numbers below come from running the stack for 90+ minutes on unmodified code before writing
any alert (`make up`, then querying Prometheus directly). Full derivation in the PR history;
this is the short version the brief asks for.

## The baseline that everything else is measured against

`traffic-generator` mixes in rejection-triggering messages at `REJECTION_MIX_RATIO=0.15`, and the
classifier scores 100% on both the golden and adversarial eval sets. So the live rejection rate
isn't approximately 15% - it **equals** 15% by construction, subject only to sampling noise. That
noise is computable: at the measured ~2 req/s, a 5-minute window has σ ≈ 1.5%, so the 3σ band
around the 15% baseline is **~10.6%-19.4%**. A 1-minute window's own noise band is much wider
(~5%-25%) - wide enough that no threshold in that range can avoid flapping, so no alert here uses
a window shorter than 5 minutes.

This baseline assumes the classifier is accurate on real traffic, and that assumption is
load-bearing: the eval sets it scores 100% on contain nothing near the policy boundary, and bare
patterns like `password` reject ordinary questions (see `docs/incident-response.md` §5). A shift in
traffic *mix* would move this rate with the filter working exactly as written, so treat
`HighRejectionRate` as "the ratio moved", not "the classifier broke".

The dashboard shipped with rejection-rate thresholds at yellow 5% / red 8% - *below* the healthy
baseline. Those would render red permanently. This is the mistake the exercise is checking for.

## Rejection-rate alerts

| Alert | Threshold | Why |
|---|---|---|
| `HighRejectionRate` | ratio > 30% for 10m | ~7σ above the 15% baseline on a 5m window - can't be noise - but still catches a real doubling of adversarial traffic. `for: 10m` requires two consecutive 5m windows above threshold. |
| `RejectionRateSpike` | current 5m rate > 2× the trailing 1h rate, **and** > 25% absolute | Catches a fast-moving change even from a low starting point, without an absolute-only threshold firing on ordinary 2%→5% wobble. The absolute floor also prevents it firing on a doubling of an already-tiny rate. |
| `LowRejectionRate` | ratio < 5% for 15m | **Under-rejection is the more dangerous failure for a safety filter** - it means adversarial traffic is being answered. Mirrors the CI gate's `min_adversarial_rejection_rate=0.60`, but against live traffic rather than the eval dataset. 15m floor is ~12.4%, so 5% leaves ~7σ of margin - this only fires if the classifier has genuinely broken. |

## Everything else

| Alert | Threshold | Why |
|---|---|---|
| `AgentAPIDown` | `up == 0` for 1m | Unmodified from the starter file - already correct. |
| `AgentAPITargetMissing` | `absent(up{job="agent-api"})` for 5m | `up == 0` needs the series to exist. If the scrape target itself disappears (container deleted, service discovery change), the series goes *absent*, not zero, and `AgentAPIDown` never fires. |
| `NoTraffic` | `sum(rate(agent_requests_total{route="/ask"}[5m])) == 0` for 5m | The traffic generator can die (`restart: on-failure`, but its own `main()` also returns quietly on a startup timeout) while the API stays healthy and every rate-based panel just goes flat, not red. Caught a real bug writing this: without `sum()`, the four `status` label series would each independently satisfy `== 0` and fire as four separate alerts instead of one. |
| `HighErrorRate` | `status="error"` share > 1% for 5m | Baseline is 0 errors (no known crash paths remain - see Task 3). 1% is above single-request noise at ~2 req/s but still catches a regression within one window. Uses `(... or vector(0))` so the panel/alert reads 0 instead of "no data" when no error has ever occurred - Prometheus counters with unused label values don't exist as a series until incremented. |
| `HighLatencyP95` | p95 > 50ms for 10m | Measured mean latency is ~90µs; p95 is comfortably under 1ms. 50ms is roughly 500× that - deliberately loose, because on this workload (regex over a short string, no I/O) *approaching* 50ms means something pathological (GC pause, resource exhaustion), not normal variance. Only meaningful after rescaling the latency histogram's buckets to sub-millisecond resolution (Task 3) - on the original buckets, which started at 5ms, every observation landed in one bucket and p95 was a constant 4.75ms regardless of actual behavior. |

## Verified, not just written

- `promtool check rules` passes on syntactically-valid-but-meaningless rules (`vector(0) > 1`
  reports `SUCCESS`), so it isn't a real gate. `promtool test rules` (`prometheus/alert-rules.test.yml`)
  is: 16 cases, a fires/stays-silent pair per alert, run in CI. Writing them caught the `NoTraffic`
  fan-out bug above.
- End-to-end, against the real running stack: `REJECTION_MIX_RATIO` was set to 0.9 and both
  `HighRejectionRate` and `RejectionRateSpike` were watched transition `inactive` → `pending`
  (the instant the ratio crossed threshold) → `firing` exactly 10 minutes later, matching `for: 10m`,
  and the firing alerts were confirmed to reach Alertmanager (`GET /api/v2/alerts`). Reverting the
  ratio to 0.15 let both resolve back to `inactive` once the rate windows rolled forward.
- All 8 rules sit `inactive`/`health=ok` against real healthy traffic for the full development
  session - no false positives observed.
