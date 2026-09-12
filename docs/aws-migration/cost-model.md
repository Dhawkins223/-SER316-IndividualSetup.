# AWS cost model

What this architecture costs per month, how each number was derived, and the
one finding the owner needs before approving any spend.

## Read this first

**AWS will cost roughly 5–10× what Railway costs today, for the same
workload.** That is not a flaw in the design below — it is what the platforms
charge.

`docs/CURRENT_INFRASTRUCTURE.md` measures the current Railway bill at
**$5–30/month**. The estimate here is **~$169/month for production** and
**~$48/month for dev**, about **$217/month combined**.

Two line items explain most of it, and neither has a Railway equivalent:

| Item | Monthly | Railway equivalent |
| --- | ---: | --- |
| Application Load Balancer | ~$18 | Included in the service |
| NAT Gateway ×2 | ~$66 | No such charge |

Those two alone are **~$84/month**, which is roughly three times the entire
current Railway bill before a single container runs.

This is stated up front because the brief says not to hide costs and not to
deploy expensive infrastructure silently. It is not an argument against the
migration — capabilities AWS provides and Railway does not (storage
autoscaling, IAM, budgets, the agent control plane) may well be worth it. But
it should be a decision made with the number in view. §6 lists what can be cut
and what it costs to cut it.

## 1. Basis and caveat

Prices are **us-east-2 list prices** and carry a real caveat: they are from
training data, not from a live API call, and AWS changes them. **Verify with
the AWS Pricing Calculator before committing to a budget.** Every figure below
shows its arithmetic so re-verification means changing one rate, not redoing
the model.

| Resource | Rate |
| --- | --- |
| Fargate vCPU | $0.04048 / vCPU-hour |
| Fargate memory | $0.004445 / GB-hour |
| Fargate Spot | ~70% off the above |
| ALB | $0.0225/hour + $0.008/LCU-hour |
| NAT Gateway | $0.045/hour + $0.045/GB processed |
| RDS db.t4g.small | $0.032/hour |
| RDS db.t4g.micro | $0.016/hour |
| RDS gp3 storage | $0.115 / GB-month |
| Secrets Manager | $0.40 / secret-month |
| ECR storage | $0.10 / GB-month |
| S3 Standard | $0.023 / GB-month |
| CloudWatch Logs | $0.50/GB ingested, $0.03/GB-month stored |
| EventBridge Scheduler | $1.00 per million invocations |
| Route 53 | $0.50 / hosted zone-month |
| Data transfer out | $0.09/GB (first 10 TB) |

730 hours per month throughout.

## 2. Production

### Web service — $36.04

2 tasks × 512 CPU (0.5 vCPU) / 1024 MiB, always on.

```
vCPU    0.5 × 2 × 730 × $0.04048  = $29.55
memory  1.0 × 2 × 730 × $0.004445 = $ 6.49
                                    ------
                                     $36.04
```

Two tasks rather than one buys a zero-downtime deploy and survival of an AZ
loss. Dropping to one saves $18.02 and gives up both.

### Always-on workers — $8.11

3 × 256 CPU / 512 MiB on **Fargate Spot**.

```
vCPU    0.25 × 3 × 730 × $0.012144  = $6.65
memory  0.5  × 3 × 730 × $0.0013335 = $1.46
                                      -----
                                       $8.11
```

The same three on-demand would be **$27.03**. Spot saves **$18.92/month** and
the risk is one lost collection cycle, which the next cadence re-collects.

### Scheduled workers — $0.98

This is the single biggest structural win in the migration.

| Worker | Runs/month | Est. seconds | Sizing | Cost |
| --- | ---: | ---: | --- | ---: |
| sports-research | 720 | 60 | 0.5 vCPU / 1 GB | $0.29 |
| research-model-refresh | 720 | 60 | 0.5 vCPU / 1 GB | $0.29 |
| settlement-worker | 720 | 60 | 0.25 vCPU / 0.5 GB | $0.15 |
| raw-retention | 720 | 60 | 0.25 vCPU / 0.5 GB | $0.15 |
| reporting-evaluation | 120 | 120 | 0.5 vCPU / 1 GB | $0.10 |
| | | | **Total** | **$0.98** |

Held resident on-demand instead, those five cost **$72.11/month**:

```
sports-research         0.5 vCPU / 1 GB × 730h = $18.03
research-model-refresh                           $18.03
settlement-worker       0.25 vCPU / 0.5 GB      = $ 9.01
raw-retention                                     $ 9.01
reporting-evaluation    0.5 vCPU / 1 GB         = $18.03
                                                  ------
                                                  $72.11
```

**Scheduling saves $71.13/month.** `reporting-evaluation` alone is $18.03 of
resident cost for about four minutes of work a month.

The 60-second estimate is the weakest assumption in this document. It is a
planning figure, not a measurement — the cycles cannot be timed while the
production database is down. Even at 5× it would be ~$5/month, so the
conclusion is robust, but re-measure once the database is recovered.

### Load balancer — ~$18

```
fixed   730 × $0.0225 = $16.43
LCU     low traffic    ≈ $ 1.50
                         ------
                          $17.93
```

There is no cheaper managed option that keeps ECS health checks, TLS
termination and a stable hostname. It is a fixed floor.

### NAT Gateway — $66.60

```
2 gateways  2 × 730 × $0.045 = $65.70
data        ~20 GB × $0.045  = $ 0.90
                               ------
                                $66.60
```

**The largest single line, and the most negotiable.** See §6.

The free S3 gateway endpoint is already configured and keeps ECR layer pulls —
the bulk of task-start traffic — off the NAT entirely. Interface endpoints for
ECR/Logs/Secrets would cost ~$7.30/month each per AZ, which at this data volume
is more than the NAT processing they would displace. They stay off.

### Database — ~$31

```
db.t4g.small  730 × $0.032   = $23.36
storage       50 GB × $0.115 = $ 5.75
monitoring    enhanced @60s  ≈ $ 2.00
                               ------
                                $31.11
```

Backup storage is free up to 100% of allocated storage, so 14-day retention on
~5 GB of data costs nothing. Performance Insights is free at 7-day retention.
Multi-AZ would roughly double the instance line to ~$46.72 and is off.

Note the storage line scales with the **autoscaling ceiling actually used**,
not the 200 GB maximum — RDS bills provisioned storage, and autoscaling only
provisions more when needed.

### Everything else — ~$8.13

| Item | Basis | Monthly |
| --- | --- | ---: |
| Secrets Manager | 5 secrets × $0.40 | $2.00 |
| ECR | ~20 images × 240 MB = 4.8 GB | $0.48 |
| S3 | artifacts + raw archive + dumps | $1.00 |
| CloudWatch Logs | ~5 GB ingest, 30-day retention | $2.65 |
| EventBridge Scheduler | 2,880 invocations | <$0.01 |
| Route 53 | 1 zone + queries | $1.00 |
| Data transfer out | dashboard traffic | $1.00 |

### Production total

| Component | Monthly |
| --- | ---: |
| Web service | $36.04 |
| Always-on workers (Spot) | $8.11 |
| Scheduled workers | $0.98 |
| Load balancer | $17.93 |
| NAT Gateway ×2 | $66.60 |
| RDS | $31.11 |
| Everything else | $8.13 |
| **Total** | **~$168.90** |

## 3. Development

No NAT Gateway — tasks run in public subnets with no inbound rules — which is
what keeps this environment affordable.

| Component | Basis | Monthly |
| --- | --- | ---: |
| Web (1 × 256/512) | 0.25 vCPU, 0.5 GB | $9.01 |
| 1 always-on worker (Spot) | 256/512 | $2.71 |
| 1 scheduled worker | hourly | $0.15 |
| Load balancer | fixed + minimal LCU | $17.43 |
| NAT Gateway | **none** | $0.00 |
| RDS db.t4g.micro + 20 GB | | $15.98 |
| Secrets (4) | | $1.60 |
| ECR / S3 / Logs | | $1.50 |
| **Total** | | **~$48.38** |

The ALB is 36% of the dev bill. If dev does not need a public hostname, running
the web task without a load balancer would cut it to ~$31.

## 4. Combined and recommended budgets

| Environment | Estimate | Recommended budget | Headroom |
| --- | ---: | ---: | --- |
| Production | $168.90 | **$180** | ~6% |
| Development | $48.38 | **$60** | ~24% |
| **Combined** | **$217.28** | **$240** | |

These are the defaults in `variables.tf` for each environment. Budget
notifications fire at 50/80/100/120% of actual plus a forecast trigger at 100%
— the forecast one is the useful member of the set, because it fires while
there is still a month left to react.

Set them **after** the first full month of real usage, not before. A budget
derived from an estimate mostly measures the estimate.

## 5. What this replaces

| | Railway today | AWS target |
| --- | ---: | ---: |
| Measured / estimated | $5–30/month | ~$217/month |
| Difference | | **+$187 to +$212/month** |

Roughly **$2,250–2,550/year**. Against that, the things AWS buys that Railway
did not:

- **Storage autoscaling.** The incident that prompted this migration was a
  fixed volume filling with no warning. RDS grows on its own.
- **A capacity alarm with room to act**, rather than discovering the problem
  when the database stops.
- **Verified automated backups.** Whether Railway snapshots exist is
  *unverified* — see database-recovery.md. That uncertainty is itself the cost
  of the current setup.
- **IAM, OIDC and per-workload secrets**, which is what the autonomous-agent
  control plane needs to be bounded at all.
- **Budgets and cost attribution**, which Railway does not break down per
  service.

Whether that is worth ~$200/month is the owner's call, and it should be made
explicitly rather than discovered on an invoice.

## 6. Levers, largest first

| Change | Saves | Cost of the change |
| --- | ---: | --- |
| `single_nat_gateway = true` | **$32.85** | Egress becomes single-AZ. A collector that loses an AZ re-collects next cadence |
| `enable_nat_gateway = false` in prod | **$66.60** | Tasks get public IPs. No inbound rules, so nothing can reach them — but it is a weaker posture than private subnets and a reviewer will ask |
| `web_desired_count = 1` | $18.02 | No zero-downtime deploy; no AZ survival |
| Drop dev entirely | $48.38 | No rehearsal environment for the cutover |
| Dev without an ALB | $17.43 | No public dev hostname |
| Workers already on Spot | *(saved $18.92)* | Already applied |
| Scheduling already applied | *(saved $71.13)* | Already applied |

The two biggest savings are already in the design. Of what remains, NAT is the
only large lever, and **`single_nat_gateway = true` is the recommended default
for this workload**: $32.85/month for AZ-redundant egress is poor value for a
research collector whose worst case is a missed cycle.

Taking that one change: **production ~$136/month, combined ~$184/month.**

## 7. What is not modelled

- **Data transfer between AZs.** Small here, but not zero.
- **The autonomous agent control plane** (§32–37 of the migration brief). SQS
  and EventBridge are cents; the agent compute is not, and it is unbounded
  until concurrency limits and per-task budgets exist. Model it separately
  before enabling it, not as a footnote to this.
- **Migration-period double-running.** Railway and AWS run in parallel through
  parity testing and stabilisation, so expect **both bills** for at least a
  month. Budget ~$250 for the overlap period.
- **Support plans.** Basic is free and assumed.
