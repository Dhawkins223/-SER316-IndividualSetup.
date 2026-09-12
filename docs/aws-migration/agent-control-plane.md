# Autonomous agent control plane — design

Design only. **Nothing here is built, and it should not be built until the
platform migration is finished and stable.** It is written now so the IAM and
network boundaries it needs are not accidentally foreclosed by decisions made
during the migration.

## 1. The premise, and the part of it that needs pushing back on

The brief asks for ~20–30 logical AI roles operating through bounded,
auditable, event-driven workflows, with 3–6 running concurrently.

The roles are fine. The number is the risk, and it is worth being direct about
why: **an agent that can open a pull request is cheap; an agent that can open a
pull request nobody reads is a liability that compounds.** Thirty roles
producing output faster than one owner can review does not multiply capacity,
it multiplies unreviewed surface area.

So the design below is shaped by one constraint that is not in the brief:
**throughput is bounded by review capacity, not by compute.** Every mechanism
here — concurrency caps, budgets, the adjudicator, the merge gate — exists to
keep generated work inside what a human can actually adjudicate.

Recommendation: start with **three** roles, not thirty. Code Review, Data
Quality, and Documentation. All three produce output whose correctness is
checkable in minutes, and all three fail safely. Add roles when the existing
ones are demonstrably paying for their review time.

## 2. Architecture

```
GitHub event / schedule
        │
        ▼
   EventBridge  ──────────────┐
        │                     │
        ▼                     ▼
   Task router          Budget guard  (rejects before spend)
   (Lambda)
        │
        ▼
   SQS role queue  (one per role, each with a DLQ)
        │
        ▼
   Agent worker  (ECS Fargate, RunTask, one task per job)
        │
        ├─► artifact ──► S3
        └─► record  ──► RDS  (agent_tasks)
        │
        ▼
   Review queue (SQS)
        │
        ▼
   Adjudicator / critic
        │
        ├─ rejected ──► recorded, ends
        └─ approved ──► draft PR / issue / report
                              │
                              ▼
                        HUMAN REVIEW  ← the actual gate
                              │
                              ▼
                            merge
```

The load-bearing property: **nothing merges without a human.** Agents open
drafts. Branch protection on `Master` is what enforces it, not agent good
behaviour — an agent that could bypass review would eventually bypass review.

## 3. Task contract

Every job is a row in `agent_tasks` and a message carrying the same fields. No
field is optional, because every one of them is a bound:

| Field | Purpose |
| --- | --- |
| `task_id` | Idempotency key. A redelivered SQS message must not re-run the work |
| `project` | Always `hawknetic-sports-tools`. Hawknetic Office is out of scope |
| `role` | Which agent, which queue, which IAM role |
| `objective` | One sentence. If it cannot be stated in one, it is not one task |
| `inputs` | Explicit refs — commit SHA, table name, S3 key. Never "look around" |
| `allowed_tools` | Allowlist. Absent means denied |
| `allowed_resources` | ARNs and repository paths the task may touch |
| `budget_usd` | Hard ceiling. Exceeded means killed, not warned |
| `max_iterations` | Loop bound |
| `timeout_seconds` | Wall-clock bound, enforced by ECS stop timeout |
| `stop_conditions` | What "done" means, checkable without a model call |
| `escalation_conditions` | What sends it to a human instead of continuing |
| `output_artifact` | S3 key of what it produced |
| `review_requirement` | `critic`, `human`, or `both` |
| `status` | `queued`/`running`/`review`/`approved`/`rejected`/`failed`/`killed` |
| `audit_log` | Every tool call, input hash, and cost, append-only |

Three of these carry most of the safety: `budget_usd`, `max_iterations`,
`timeout_seconds`. An agent without all three can spend without limit — and
the failure mode is not one expensive task, it is a retry loop that bills all
night.

## 4. Permissions

One IAM role per agent role. Never a shared one.

| Role class | May | May not |
| --- | --- | --- |
| Research | Read research tables; write to `artifacts/` | Write operational tables |
| Engineering | Read repo; push a branch; open a draft PR | Push to `Master`; merge; approve |
| Security | Read infra config and scan results; write findings | Change infra |
| Ops / FinOps | Read metrics, logs, Cost Explorer | Change infrastructure |
| Deploy | Deploy an already-approved artifact | Build artifacts; change task definitions |
| Adjudicator | Read artifacts; write verdicts | Produce the work it judges |

No agent role gets, under any circumstance:

- Organizations or billing administration
- `iam:*` or the ability to modify its own role
- Unscoped Secrets Manager access
- Production database writes
- Route 53 / DNS
- Any `Delete*` on a production resource
- Merge or approve on any pull request

Separation that matters most: **the adjudicator must not be able to write the
artifacts it reviews.** A critic that can edit its way to approval is not a
critic.

## 5. Bounds

| Bound | Value | Enforced by |
| --- | ---: | --- |
| Concurrent agent tasks | 3 | ECS service quota + router check |
| Per-task budget | $2.00 | Router pre-check + in-task accounting |
| Daily platform budget | $25.00 | Budget guard; rejects at the queue |
| Per-task wall clock | 15 min | ECS stop timeout |
| Max iterations | 10 | In-task counter |
| Open agent PRs | 5 | Router refuses to queue beyond this |

That last one is the review-capacity bound made concrete. When five agent PRs
are open, the system stops producing work and waits — which is the correct
behaviour, and the one most likely to be removed by someone frustrated with it.

`start small` is not a slogan here: concurrency goes from 3 to 6 only after a
measured month showing review keeps pace.

## 6. Research agent rules

Research agents inherit the repository's existing research-only posture, and
the AWS migration preserves it in both the image defaults and the task
definitions.

They **may**: gather data, build features, compute no-vig probabilities and EV,
run Monte Carlo and correlation analysis, backtest, measure calibration,
compare model variants.

They **may not**: place wagers, execute trades, upload orders, or promote a
model into production. `MODEL_PROMOTION_ENABLED=false` stays false, and
promotion stays a human act.

Method constraints, which exist because an agent optimising a metric will find
the cheapest way to move it:

- **Held-out data stays held out.** An agent that can read the evaluation set
  will overfit to it, and the resulting number will be indistinguishable from
  a real improvement until it is deployed.
- **Forward-only logs.** No backfilled predictions.
- **A claimed improvement needs a sample size and an interval**, not a point
  estimate. "Better" without a confidence statement is not a result.
- **Repeated testing against the same validation sample is itself overfitting.**
  Track how many times a sample has been used and retire it.
- **No performance claim without the losing runs attached.**

## 7. Auditability

Every task writes an append-only audit record: inputs and their hashes, every
tool call, artifact digests, token and dollar cost, the critic's verdict, and
the human decision. Records are immutable and retained.

The question this must answer, months later: *why does this line of code, this
model, this number exist?* If the audit log cannot answer that, the control
plane has failed regardless of what it produced.

## 8. Cost

Not modelled in `cost-model.md`, deliberately — the infrastructure is cents and
the agent compute is not:

| Component | Monthly |
| --- | ---: |
| SQS, EventBridge, Lambda router | < $2 |
| Agent task compute (3 concurrent, bounded) | $10–40 |
| Model inference | **Unbounded without the budget guard** |
| S3 artifacts | ~$1 |

The middle row is the whole point of §5. Infrastructure cost here is
negligible; inference cost is the only line that can run away, and it does so
quietly.

## 9. Build order

Not before the migration is stable. Then:

1. `agent_tasks` schema, forward-only migration. No agents yet.
2. Router and budget guard. Verify it **rejects** over-budget tasks — test the
   refusal, not the happy path.
3. One role end to end: Documentation. Lowest blast radius; a bad docs PR is
   embarrassing, not dangerous.
4. Adjudicator, with the artifact/verdict separation enforced by IAM.
5. Code Review and Data Quality.
6. Measure a month: review time per PR, acceptance rate, cost per accepted
   change.
7. Expand only if that month says the work is worth reviewing.

Step 6 is the one that will be skipped under pressure. It is the only step that
answers whether any of this should exist.
