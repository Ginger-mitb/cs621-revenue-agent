# CS621 group project — revenue analysis agent on theLook

Read this first in any new session. It records decisions that were made in
conversation and are not derivable from the code. The course-level rules in
`../CLAUDE.md` (Anaconda interpreter, no key files, etc.) still apply.

## What the project is

An agent that answers "why did revenue change?" on a frozen e-commerce
snapshot, by **generating hypotheses and testing each one against the data**
(the metric tree: revenue = orders × AOV; orders = sessions × conversion;
profit = revenue − cost; each node split by segment). It is measured with the
Week 3 harness against three baselines: a single LLM call, a recursive
drill-down **script**, and "escalate everything". Later it gains two or three
**mock gated actions** (publish report / notify ops / pause listing) so the
Week 8 autonomy question has something to bite on.

The honest expected finding: the script wins on single-dimension drivers; the
agent must earn its cost on mechanism chains, ambiguous definitions, and
"no driver" cases. Report that split; do not hide it.

Why this direction was chosen over the after-sales agent: it does not
duplicate the lecturers' RefundBot example, and hypothesis generation gives
Week 5 (planning) the strongest story. Decision rule agreed: if a 3-scenario
pilot shows the script matching the agent everywhere, switch to after-sales.

## Data (do not re-export, do not modify the base)

- `data/snapshot/*.parquet` + `manifest.json` = the **frozen snapshot**
  (theLook eCommerce, cutoff **2026-09-01**, exported 2026-09-17, 7 tables,
  events pre-aggregated into `sessions`). This is what gets committed/shared.
- `data/thelook.sqlite` (184 MB) is **derived**; rebuild with `buildsql.py`.
  Never commit it. Open it read-only: `file:...?mode=ro`.
- The agent's "today" is **2026-09-01**. Last complete month = 2026-08.
- The data is synthetic (Google Looker demo). Base data has **no real
  drivers**: 7→8月 is pure organic growth, every segment lift ≈ 1. Findable
  drivers must be planted. Full audit in `eda.py` output and the published
  page "theLook 数据体检".

## Reference library (the referee) — `reference/`

- `KPI_DEFINITIONS.md` — 20 stipulated rules (R1–R20). Agent retrieves it,
  `kpi.py` implements it, grader judges by it. Change a rule → change
  `kpi.py` → rerun tests → regenerate truth.
- `kpi.py` — `KPI(db_path)`: monthly(), revenue(m), decompose(prev,cur),
  contribution(dim,prev,cur,measure), find_drivers(prev,cur,dims,measure),
  zscore(), funnel(), leakage(month, by=dim), last_complete_months(n),
  previous_period(). `measure` is "revenue" (default) or "net_revenue".
  Takes ANY db path, so scenario databases get their truth with the same code.
- `queries/*.sql` — independent SQL path (two files are wrong on purpose:
  `trap_item_month.sql`, `trap_fanout.sql`).
- `test_kpi.py` — 25 checks, all passing: cross-path, invariants, pinned
  numbers for this snapshot, data quirks. Run before trusting anything.
- `make_truth.py [db] [out.json]` — freezes ground truth. `truth/base.json`
  exists. Truth is generated BEFORE the agent runs and never edited after
  looking at agent output. Fields: `planted` (what the generator did, with an
  `expect` block), `last_month.drivers` / `net_drivers` (what the referee
  flags — NOT the same thing, see R11 fragility below), `leakage_by_dist_center`
  for the last and previous month, `monthly.*_share` (R21).

Thresholds (in `kpi.py`): unusual if |z| ≥ 2.0 vs the 24 months before the
period; driver if |share of change| ≥ 15% AND |lift − 1| ≥ 0.5; skip driver
search if total change < 2% of base. Aug 2026 sits on a knife edge (z 1.99 vs
2.06 depending on baseline) — never build a case on it.

## Decisions already made

- Defaults when the user does not say: period = last complete month;
  comparison = previous period of equal length; revenue = gross by ORDER
  month; "unusual" per R10. Put defaults in the **tool layer** (optional
  params, echoed back in results), not only in the prompt.
- Open-ended questions ("analyse the last 3 months") → describe, then judge
  whether anything is unusual, then and only then hunt drivers; stop when
  marginal value collapses. Structured output with a `drivers` list (empty =
  no driver) so the grader can check it deterministically.
- Graders: `all_present` for required elements; recompute every quoted
  number with `kpi.py` (relative tolerance 0.5%); re-run the agent's own
  queries to measure the **unsupported-claim rate**; negative check on
  invented causes.
- Case tags planned: single_dim, mechanism_chain, mix_shift, decoy,
  no_driver, truncated_data, ambiguous_kpi, fanout_trap, open_ended,
  vocab_gap.

## Scenarios — `scenarios/make_scenarios.py` (S01–S03 done 2026-09-18)

```
& "C:\Users\Ting\anaconda3\python.exe" scenarios\make_scenarios.py          # all, ~3 min each
& "C:\Users\Ting\anaconda3\python.exe" scenarios\make_scenarios.py S02      # one
```
Copies the base db to `scenarios/<id>.sqlite` (190 MB each, never commit),
plants, verifies, then freezes `reference/truth/<id>.json` and updates
`scenarios/manifest.json` (event, params, sha256, margins). Deterministic:
seed `[20260901, n]`, a rerun gives the same sha256. Verification = the 18
non-pinned `test_kpi` checks on the scenario db + month-by-month arithmetic
(base + planted == scenario, exactly) + threshold margins (share ≥ 30%,
|lift−1| ≥ 1, |z| ≥ 3 when the total moves). Truth is only written if all pass.

Primitives, each cascading through every table that would have noticed:
`clone_orders` (users if `new_users`, order_items, inventory_items, one purchase
session per item), `flip_to_returned` (orders + order_items status and stamps),
`drop_orders` (order_items, and the user's nearest purchase session; smoke-tested,
unused so far). Sessions carry no order id, so item↔session is matched by
user + time; inventory rows referenced by items all have `sold_at` NULL in the
base, so there is nothing to sync there.

| id | tag | what was planted | referee sees |
|---|---|---|---|
| S01 | single_dim | Aug 2026 Facebook acquisition campaign: 704 new users (cloned from Facebook-acquired users, signed up in Aug) each place one order → Facebook orders ×3, +54,209 revenue | z +3.63 (unusual), gross drivers = [Facebook] share 0.43 lift 8.8, nothing else flagged |
| S02 | mechanism_chain | Chicago IL DC mislabelled batch since Jul 2026: 80% of the non-cancelled orders whose items all ship from Chicago flipped to Returned (340 in Jul, 344 in Aug; −20k net each month) | gross unchanged, gross drivers = [], net drivers = base noise only; returned share Chicago 45% / 41% vs ≤ 13.5% elsewhere and ~10% every earlier month; overall returned share 15.8% vs ~10% |
| S03 | no_driver | nothing (byte-identical copy) | drivers = [], z 1.99 |

Why S01/S02 differ from the first plan (remove 60% Facebook / 35% Chicago
returns from Jul): the numbers did not clear the thresholds.
- Aug already grows +20% (z 1.99, history mean 5% sd 7.7%), so no removal
  short of a third of the month makes it unusual, and −60% Facebook gives share
  −14% (< R11's 15%). Hence a surge, cloned as **new users**: cloning repeat
  orders makes every clone "returning" and tips customer_type over R11 too.
- Chicago's "Complete" orders are 15k/month; 35% of that is 1.5% of net revenue.
  Even 80% of everything it ships (20k) hit in Aug only shrinks the Jul→Aug net
  change from 62k to 42k, which multiplies every other segment's share by 1.5
  and flags seven organic segments. Planting Jul AND Aug keeps the change tests
  quiet and makes the signal a **level** (returned share by DC, R21 added).
  The "unusual" gate (R10) does not fire for S02; its eval question must be
  about net/returns or open-ended with leakage in the describe step.

**R11 fragility, found while planting (team decision pending):**
1. On the base, R11 on NET revenue Jul→Aug already flags Philadelphia PA
   (share 0.196, lift 2.24) — noise. On gross the same DC is a hair under
   (0.143 / 1.45). Returning customers on net sit at |lift−1| = 0.44.
   Pinned in `test_net_revenue_on_base_has_one_noise_driver`.
2. share_of_change is unstable when a plant offsets organic growth (shrinking
   denominator). Any negative plant in a growth month inherits this.
3. Consequence for eval cases: require the **planted** driver from `planted[].expect`,
   tolerate other entries the referee flags, never require Philadelphia.
   Consider a more robust R11 (e.g. excess contribution vs base revenue) before
   building mix_shift / decoy scenarios.

## Eval cases and the script baseline (done 2026-09-20)

- `evals/harness.py` — unchanged copy of the Week 3 harness (`agentlab/evals.py`)
  so team members without the lab folders can run it.
- `evals/cases.py` — 8 cases, every number read from the truth files:

  | id | tag | db | question |
  |---|---|---|---|
  | C01 | single_dim | S01 | 8月收入为什么涨了这么多？ |
  | C02 | mechanism_chain | S02 | 8月的经营情况怎么样，有什么要注意的？ |
  | C03 | no_driver | S03 | 8月收入为什么变了？ |
  | C04 | open_ended | S03 | 帮我看看最近三个月。 |
  | C05 | ambiguous_kpi | S03 | 哪个渠道涨得最多？ |
  | C06 | vocab_gap | S03 | 最近三个月流水多少，客单价呢？ |
  | C07 | truncated_data | S03 | 9月到现在收入怎么样？ |
  | C08 | fanout_trap | S03 | 8月收入多少？ |

  Tags `fixed_question` (C01–C03, C08) vs `question_variation` (C04–C07) are
  the two columns of the 2×2 the report is built on. S02's question is NOT held
  out (decided 2026-09-20).
- **OUTPUT_CONTRACT** (in cases.py): every competitor ends its answer with one
  JSON object `{period, measure, value, change_pct, unusual, drivers, assumptions,
  answer}`. Graders read the JSON first, fall back to prose for numbers; a driver
  list only counts from JSON. Whoever writes the agent prompt pastes the contract in.
- Graders: `quotes(**numbers)` 0.5% tolerance (0.02 abs for R6's two AOVs),
  `drivers(required, tolerated)` — required from `planted[].expect`, tolerated =
  whatever the referee flags (Philadelphia PA), anything else fails —
  `no_invented_cause` (R18/R20 words), `unusual_is`, `period_is`, `says_any`,
  `not_number` (known wrong answers). `diagnose()` names which known mistake a
  failing answer made.
- `evals/cases.py --oracle` — grader self-test: 8 reference answers must pass,
  13 wrong answers must fail. Run it whenever a grader changes.
- `baselines/script.py` — the drill-down script, `--thorough` adds net drivers +
  returned-share history + by-DC. `evals/cases.py --script [--thorough]` grades it.
  **Result: both variants 3/8** — pass C01, C03, C08; fail C02 and all four
  question-variation cases. The thorough variant misses C02 by 0.0003 because
  July (also planted) sits inside its own 12-month history; excluding July gives
  z 9.2. Left as is: the eval reports both variants and their settings, the
  script is not tuned to lose or to win.

## NEXT STEP: tool layer, agent, single-LLM baseline, pilot

1. Tool layer over `kpi.py` with defaults echoed back (R8/R9) and a raw SQL
   tool (for C08). `describe(period)` must return gross, net, orders, AOV AND
   returned/cancelled share together, otherwise the agent walks
   "z 1.99 → not unusual → stop" on C02 exactly like the naive script.
2. Agent: hypothesis loop (describe → judge → hypotheses → test each with the
   tools → structured answer per OUTPUT_CONTRACT). Needs a working Groq setup
   from the labs; `AGENTLAB_FAKE=1` is only for plumbing.
3. Single-LLM-call baseline: one call with the KPI doc + a fixed context pack
   (monthly table + top-5 contributions); no tools.
4. Run all three through `run_cases(factory, n_runs=3)`, report per tag with
   `Report.render(baseline=...)`. Expected: script wins fixed_question, agent
   must win question_variation and hold no_driver. Put that 2×2 in the proposal.

## Conventions

- Run from this directory with the Anaconda interpreter.
- Scenario dbs live in `scenarios/`, their truth in `reference/truth/`.
  Regenerate them from the script; never hand-edit a scenario db or its truth.
- SQLite here has no indexes: a correlated subquery over orders × order_items
  never finishes. Join, or do it in pandas.
- `explore.py` = EDA walk of the metric tree; `eda.py` = data audit. Both
  read-only, both fine to rerun.
- Course notes for context: Week 2/3/4 pages in the artifact gallery; lab
  code in `../session4/week4_stu/agentlab/` (evals.py is the harness).
