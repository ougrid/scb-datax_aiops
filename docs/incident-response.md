# Incident response: rejection rate spike

Scenario: 3am, `HighRejectionRate` and/or `RejectionRateSpike` has fired (see
`prometheus/alert-rules.yml`, thresholds explained in `docs/alerting-rationale.md`). You don't
know this system. This is the procedure.

## 1. Initial triage (first 2 minutes)

**Is this real, or an alert bug?** Open Grafana (`http://localhost:3000`, admin/admin) →
"Agent API Monitoring" dashboard. Check three panels together:

- **API Status** - is `up{job="agent-api"}` actually 1? If it's down, this isn't a rejection
  incident, it's `AgentAPIDown`/`AgentAPITargetMissing` - different runbook, not this one.
- **Request Rate** - is traffic flowing at all? If it's flat zero, `NoTraffic` should also be
  firing; a rejection-rate ratio computed from near-zero requests is noisy, not necessarily a
  real spike.
- **Rejection Rate by Reason** / **Rejections by Reason (rate/s)** - this is the answer to the
  question that matters most right now:

> **Is the classifier correctly rejecting a wave of adversarial traffic, or is it incorrectly
> rejecting legitimate traffic?**

That single fork decides everything that follows. You cannot answer it from the alert
notification alone - you have to look at *what* is being rejected.

```bash
# Which category is driving it, right now
curl -s --get 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=sum by (reason) (rate(agent_rejections_total[5m]))'
```

- Dominated by `prompt_injection` / `secrets_request` / `dangerous_action`, and the messages
  genuinely look adversarial when you sample them (§2) → **attack traffic**, filter working as
  designed.
- Rejections spread across categories, or concentrated in one category but the sampled messages
  look like normal user questions → **false-positive regression**, the filter is broken.

## 2. Investigation

Commands for *this* system, not generic advice - copy-pasteable.

**Did we just deploy?** The single highest-value check, because most self-inflicted incidents
are a deploy that just happened.

```bash
curl -s --get 'http://localhost:9090/api/v1/query' --data-urlencode 'query=agent_build_info'
```

Compare the `git_sha` against `deployment/manifest.yml`'s history table. If a deploy landed in
the last hour, treat it as the prime suspect until ruled out.

**Check recent `/ask` traffic directly** for volume and timing context:

```bash
docker compose logs agent-api --since 15m --tail 50
```

This will **not** tell you which requests were rejected - every `/ask` call logs as
`"POST /ask HTTP/1.1" 200 <bytes>"` regardless of outcome (rejection is a 200 with a JSON body,
not an HTTP-level signal), and the message content itself isn't logged at all. Verified: grepping
these logs for "reject" matches nothing. If you need to know what a specific message actually
was, you don't have it retroactively - known gap, see §5. Reproduce going forward instead, next.

**Reproduce a classification decision directly**, using a message you believe should NOT be
rejected but is being rejected (false-positive case), or one you believe SHOULD be rejected to
confirm the filter still works (under-rejection case):

```bash
curl -s -X POST http://localhost:8080/ask -H 'Content-Type: application/json' \
  -d '{"message": "<the message in question>"}'
```

**Check whether the classifier as a whole still behaves correctly** against known-good and
known-bad datasets - the fastest single "is it us or them" discriminator available:

```bash
docker compose run --rm eval-runner
```

Golden accuracy dropping below 100% or golden rejection rate above 0% → the classifier is now
rejecting things it shouldn't (false-positive regression). Adversarial rejection rate dropping
below 100% → the classifier is failing to reject things it should (the more dangerous
direction - see `LowRejectionRate` in the alerting rationale).

**Check for a spike vs. a sustained shift** - was this always this way, or did it just change:

```bash
curl -s --get 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=sum(rate(agent_rejections_total[5m])) / sum(rate(agent_requests_total{route="/ask"}[5m]))'
curl -s --get 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=sum(rate(agent_rejections_total[1h])) / sum(rate(agent_requests_total{route="/ask"}[1h]))'
```

A 5m rate several times the 1h rate (roughly the `RejectionRateSpike` comparison itself)
confirms a genuine step change, not a slow drift you've been ignoring for hours.

**Is it broad or from one source?** Honest limitation: **this system does not label requests by
client/IP**, so you cannot currently distinguish "one misbehaving client hammering rejection
triggers" from "broad traffic shift" via metrics alone. `docker compose logs agent-api` gives
request timestamps but not caller identity beyond the Docker network IP. Known gap - see §5.

## 3. Mitigation vs. escalation

| Situation | Action | Escalate? |
|---|---|---|
| Attack traffic, rejections correct, volume manageable | No code change. Monitor. If sustained, consider rate-limiting at a layer above this API (out of scope for this repo - it has no rate limiter today). | No, unless volume threatens availability - then page infra/on-call for rate-limiting help. |
| False-positive regression tied to a recent deploy | **Roll back.** Redeploy the previous `image_tag` from `deployment/manifest.yml`'s history table (`docker compose up -d --build` with that SHA checked out), confirm `docker compose run --rm eval-runner` returns to 100%/0%/100%, then fix forward on a branch. | No, unless the deploy also affected other services. |
| False-positive regression with no recent deploy | Something environmental changed (traffic pattern, a new legitimate-but-unusual message shape). Add the false-positive message to the eval runner's golden dataset once triaged, but don't change the classifier at 3am without a test - a live regex edit with no eval run is how you cause the *next* incident. | Escalate to whoever owns the classifier/prompt if the pattern isn't obvious within ~15 minutes. |
| **`LowRejectionRate` firing (adversarial rejection rate has dropped), regardless of `HighRejectionRate`'s state** | Treat as a **security incident**, not a metrics quirk. Do not wait for business hours. Confirm with `docker compose run --rm eval-runner` - if adversarial rejection rate is below 60%, the filter has materially degraded. | **Yes, immediately.** Page security/on-call lead. This is the one case in this table where "wait and see" is the wrong default. |
| Can't determine cause within 15 minutes | Don't keep flailing alone. | Escalate - a fresh set of eyes, or the person who owns this classifier, is faster than continuing to guess. |

**Rollback command reference** (adjust the SHA to the target row from
`deployment/manifest.yml`'s history table):

```bash
git checkout <previous-good-sha> -- agent-api
docker compose up -d --build agent-api
docker compose run --rm eval-runner   # confirm gates pass before declaring the rollback done
```

**Who gets woken, by severity** (`alertmanager/alertmanager.yml`): `critical` routes to the paging
receiver and repeats hourly until resolved — `AgentAPIDown`, `AgentAPITargetMissing`,
`LowRejectionRate`, `HighErrorRate`, `AlertmanagerDown`. `warning` routes to chat and repeats every
4h — `HighRejectionRate`, `RejectionRateSpike`, `NoTraffic`, `HighLatencyP95`. If a page woke you,
it was one of the first group.

**Resolution criteria** — you can stop watching when all four hold:

1. The driving rule is back to `inactive` in Prometheus (`/alerts`), not merely quiet. `for:`
   durations mean the firing state lags recovery by 10-15m, so read the rule state rather than
   inferring from silence.
2. `docker compose run --rm eval-runner` passes all three gates.
3. The 5m rejection rate has been inside the 10.6%-19.4% baseline band for 15 minutes. One window
   is not evidence.
4. If you rolled back, `agent_build_info` reports the SHA you intended, not the bad one.

If 1-3 hold but you never found the cause, that is an incident with a quiet symptom, not a fixed
one. Hand it over rather than closing it.

## 4. Post-incident

- **Reconstruct the timeline** from Prometheus (it retains history across the incident window):
  when the ratio crossed the alert threshold, when it returned to baseline, whether `for:`
  durations matched what you'd expect. This is also how you validate the alert itself behaved
  correctly - see `docs/alerting-rationale.md` for how thresholds were derived and verified.
- **Did the alert fire at the right time, with enough lead time to matter?** If not, that's a
  threshold problem worth revisiting - but resist the urge to loosen a threshold purely because
  it correctly caught something real and someone found that inconvenient. A true positive that
  woke someone up is not evidence the threshold is wrong.
- **Add the incident's messages to the eval datasets** (`eval-runner/runner.py`) if a
  classification gap was found - false positives to the golden dataset, missed adversarial
  patterns to the adversarial dataset - so the same gap fails CI next time, not just this alert.
- **Note the observability gap this incident revealed**, if any (see §5 for the ones already
  known). Every incident that required guessing at something the metrics couldn't answer is a
  concrete argument for a specific new metric or log field - more convincing than a hypothetical
  ask.

## 5. Known gaps (read before you assume the metrics can answer something they can't)

- **No per-client/per-IP labeling.** Cannot distinguish one bad actor from a broad shift using
  metrics alone; `docker compose logs` gives timestamps and Docker-network IPs only.
- **No request/response body logging.** The rejected message that triggered a false positive
  isn't recoverable after the fact - the access log line has the HTTP method/path/status, not
  the body - so you can only reproduce going forward, not retroactively.
- **Single-instance Prometheus, no long-term storage.** History is limited to local retention;
  there's no long-term store to query "what did this look like a month ago."
- **No rate limiting or auth on `/ask`.** An attack alert can identify the problem but this
  service has no built-in lever to actively throttle it - mitigation for a genuine flood
  requires a layer that doesn't exist in this repo yet.
- **No external heartbeat for the alert pipeline.** `AlertmanagerDown` covers Alertmanager
  crashing while Prometheus still scrapes it, but nothing catches Prometheus itself dying or the
  scrape target disappearing - a dead monitoring stack looks identical to silence. The standard fix
  is a Watchdog alert routed to an external heartbeat service; not built here.
- **The classifier's false-positive rate is not measurable from metrics.** Live traffic has no
  ground truth, so no counter separates a correct rejection from a wrongly-rejected real user -
  the question §1 opens with. Worse, the eval gate cannot detect it either: the golden dataset is
  the same 20 messages the traffic generator sends as normal traffic, none of them near the policy
  boundary. Spot-checking seven ordinary questions that sit near it - including "How do I reset my
  password?" - all seven were rejected. Treat a 0% golden rejection rate as "the dataset is easy",
  not "the filter is precise", and when triaging a spike, sample the actual messages (§2) rather
  than trusting the ratio.
