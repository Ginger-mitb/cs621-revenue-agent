"""evals/cases.py -- the eval cases: question x scenario db x expected answer x grader.

    & "C:\\Users\\Ting\\anaconda3\\python.exe" evals\\cases.py --show              # list the cases
    & "C:\\Users\\Ting\\anaconda3\\python.exe" evals\\cases.py --script            # grade the script baseline
    & "C:\\Users\\Ting\\anaconda3\\python.exe" evals\\cases.py --script --thorough

Every expected number is read from the frozen truth files (reference/truth/*.json),
never typed by hand and never taken from an agent's output. Each case carries the
db it runs on, so an agent is built per db with `run_cases(agent_factory, ...)`.

Any competitor (agent, script, single LLM call) must end its answer with one JSON
object that follows OUTPUT_CONTRACT. The graders read that object first and fall
back to the prose for numbers, so a competitor that cannot produce JSON still
gets credit for quoting the right figure, but never for a driver list.

Grading principles (group project/CLAUDE.md):
  * required drivers come from `planted[].expect`; anything else the referee
    happens to flag (R11 noise such as Philadelphia PA on net revenue) is
    tolerated, never required; any other driver fails the case
  * every quoted number is checked against the referee with 0.5% tolerance
    (tighter where the trap is a small difference, e.g. R6's two AOVs)
  * no-driver cases fail on invented causes (R18/R20 vocabulary)
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.harness import AgentResult, Case, Report, _norm, run_eval   # noqa: E402

TRUTH_DIR = ROOT / "reference" / "truth"

OUTPUT_CONTRACT = """End your answer with exactly one JSON object on its own line, for example:
{"period": ["2026-08"], "measure": "revenue", "value": 519369.9, "change_pct": 0.2034,
 "unusual": false,
 "drivers": [{"dimension": "acq_source", "segment": "Facebook", "measure": "revenue"}],
 "assumptions": ["channel = acquisition source (users.traffic_source)"],
 "answer": "one short paragraph in the user's language"}
Rules: "drivers" is [] when there is no driver. "value" is null when the period has
no data. "dimension" is one of customer_type, acq_source, country, department,
category, brand, dist_center. "unusual" refers to the headline measure (R10)."""


@dataclass
class DbCase(Case):
    db: str = ""          # scenario db, relative to the project root
    truth: str = ""       # its frozen truth file


# ---------------------------------------------------------------- reading the answer

def parse_json(output: str) -> dict | None:
    """The answer object: the JSON dict that ends LAST in the output, outermost if
    nested (a driver entry inside it must not be mistaken for it). Text after it,
    such as a closing code fence, is ignored."""
    dec = json.JSONDecoder()
    best = None                                   # (end, -start, obj)
    for m in re.finditer(r"\{", output):
        try:
            obj, n = dec.raw_decode(output[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            key = (m.start() + n, -m.start())
            if best is None or key > best[0]:
                best = (key, obj)
    return best[1] if best else None


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    return [float(x) for x in _NUM.findall(_norm(text))]


def _close(a, b, rel: float, abs_tol: float) -> bool:
    try:
        return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=abs_tol)
    except (TypeError, ValueError):
        return False


def has_number(text: str, value: float, rel: float = 0.005, abs_tol: float = 0.0) -> bool:
    """Is `value` quoted anywhere in the text, in any formatting? Percentages may be
    given as 0.2034 or 20.3(%)."""
    forms = [value] + ([value * 100] if abs(value) < 1 else [])
    return any(_close(n, f, rel, abs_tol) for n in numbers_in(text) for f in forms)


# ---------------------------------------------------------------- graders

Grader = Callable[[Case, AgentResult], bool]


def quotes(**expected) -> Grader:
    """Every keyword must be quoted: as that JSON field, or anywhere in the prose.
    A value may be (number, abs_tol) for a tighter check."""
    def _g(case: Case, result: AgentResult) -> bool:
        j = parse_json(result.output) or {}
        for key, want in expected.items():
            want, abs_tol = want if isinstance(want, tuple) else (want, 0.0)
            rel = 0.0 if abs_tol else 0.005
            in_json = key in j and j[key] is not None and _close(j[key], want, rel, abs_tol)
            if not (in_json or has_number(result.output, want, rel, abs_tol)):
                return False
        return True
    return _g


def not_number(value: float, abs_tol: float = 0.0) -> Grader:
    """The answer must NOT contain this figure (a known wrong answer)."""
    def _g(case: Case, result: AgentResult) -> bool:
        return not has_number(result.output, value, 0.0 if abs_tol else 0.005, abs_tol)
    return _g


def drivers(required=(), tolerated=()) -> Grader:
    """The JSON driver list must contain every `required` (dimension, segment) and
    nothing outside required + tolerated. Segment match is case-insensitive substring,
    so "Chicago" matches "Chicago IL". No JSON = no driver list = fail."""
    def match(got, want):
        return got[0] == want[0] and want[1].lower() in got[1].lower()

    def _g(case: Case, result: AgentResult) -> bool:
        j = parse_json(result.output)
        if j is None or not isinstance(j.get("drivers"), list):
            return False
        got = [(str(d.get("dimension", "")), str(d.get("segment", ""))) for d in j["drivers"] if isinstance(d, dict)]
        if any(not any(match(g, w) for g in got) for w in required):
            return False
        allowed = list(required) + list(tolerated)
        return all(any(match(g, a) for a in allowed) for g in got)
    return _g


def unusual_is(flag: bool) -> Grader:
    def _g(case: Case, result: AgentResult) -> bool:
        j = parse_json(result.output) or {}
        return j.get("unusual") is flag
    return _g


def period_is(months: list[str]) -> Grader:
    def _g(case: Case, result: AgentResult) -> bool:
        j = parse_json(result.output) or {}
        p = j.get("period")
        return [p] == months if isinstance(p, str) else p == months
    return _g


def says_any(*phrases: str) -> Grader:
    def _g(case: Case, result: AgentResult) -> bool:
        out = _norm(result.output)
        return any(_norm(p) in out for p in phrases)
    return _g


INVENTED_CAUSES = ("促销", "折扣", "降价", "优惠", "旺季", "淡季", "季节", "节日", "假期", "周末", "物流", "延迟", "配送延误",
                   "promotion", "discount", "sale price", "season", "holiday", "weekend", "delivery delay",
                   "campaign", "marketing", "advertis")


def no_invented_cause(case: Case, result: AgentResult) -> bool:
    """R18/R20: the base data has no discounts, no seasonality, no weekday effect and
    no delivery delays. Naming one is invention, whatever the numbers say."""
    out = result.output.lower()
    return not any(w in out for w in INVENTED_CAUSES)


def all_of(*graders: Grader) -> Grader:
    def _g(case: Case, result: AgentResult) -> bool:
        return all(g(case, result) for g in graders)
    return _g


# ---------------------------------------------------------------- the cases

def _truth(sid: str) -> dict:
    return json.loads((TRUTH_DIR / f"{sid}.json").read_text(encoding="utf-8"))


def _flagged(t: dict, section: str) -> list[tuple[str, str]]:
    """Everything the referee flags in that section, gross and net. Tolerated, not required."""
    return [(d["dimension"], d["segment"]) for d in t[section]["drivers"] + t[section]["net_drivers"]]


def build_cases() -> list[DbCase]:
    s1, s2, s3 = _truth("S01"), _truth("S02"), _truth("S03")
    lm1, lm2, lm3, l3 = s1["last_month"], s2["last_month"], s3["last_month"], s3["last_3_months"]
    wrong = s3["known_wrong_answers"]
    chicago = s2["planted"][0]["expect"]
    facebook = s1["planted"][0]["expect"]

    def case(cid, sid, question, tags, grader, expected):
        return DbCase(id=cid, input=question, expected=expected, tags=tags, grader=grader,
                      db=f"scenarios/{sid}.sqlite", truth=f"reference/truth/{sid}.json")

    return [
        case("C01", "S01", "8月收入为什么涨了这么多？", ["single_dim", "fixed_question"],
             all_of(quotes(value=lm1["headline"]["revenue"], change_pct=lm1["vs_previous_month"]["delta_pct"]),
                    unusual_is(True),
                    drivers(required=[(facebook["dimension"], facebook["segment"])],
                            tolerated=_flagged(s1, "last_month"))),
             f"revenue {lm1['headline']['revenue']:,.0f} ({lm1['vs_previous_month']['delta_pct']:+.1%}), unusual, "
             f"driver = {facebook['segment']} only"),

        case("C02", "S02", "8月的经营情况怎么样，有什么要注意的？", ["mechanism_chain", "fixed_question"],
             all_of(quotes(value=lm2["headline"]["revenue"]),
                    says_any("退货", "退回", "return"),
                    quotes(returned_share=lm2["leakage"]["returned"]),
                    drivers(required=[(chicago["dimension"], chicago["segment"].split()[0])],
                            tolerated=_flagged(s2, "last_month"))),
             f"gross {lm2['headline']['revenue']:,.0f} is ordinary growth; returned share "
             f"{lm2['leakage']['returned']:.1%} vs ~10% history; {chicago['segment']} "
             f"{lm2['leakage_by_dist_center'][chicago['segment']]['returned']:.0%} returned"),

        case("C03", "S03", "8月收入为什么变了？", ["no_driver", "fixed_question"],
             all_of(quotes(value=lm3["headline"]["revenue"], change_pct=lm3["vs_previous_month"]["delta_pct"]),
                    unusual_is(False),
                    drivers(required=[], tolerated=_flagged(s3, "last_month")),
                    no_invented_cause),
             f"revenue {lm3['headline']['revenue']:,.0f} ({lm3['vs_previous_month']['delta_pct']:+.1%}), "
             f"z {lm3['zscore']}: within normal variation, no driver"),

        case("C04", "S03", "帮我看看最近三个月。", ["open_ended", "question_variation"],
             all_of(period_is(l3["period"]),
                    quotes(value=l3["headline"]["revenue"], change_pct=l3["vs_previous_3_months"]["delta_pct"]),
                    drivers(required=[], tolerated=_flagged(s3, "last_3_months")),
                    no_invented_cause),
             f"{l3['period']}: revenue {l3['headline']['revenue']:,.0f} ({l3['vs_previous_3_months']['delta_pct']:+.1%} "
             f"vs the 3 months before); only {l3['unusual_months']} unusual; no driver"),

        case("C05", "S03", "哪个渠道涨得最多？", ["ambiguous_kpi", "question_variation"],
             all_of(says_any("Search"),
                    says_any("获客", "acquisition", "users.traffic_source"),
                    says_any("会话", "访问来源", "session", "sessions.traffic_source")),
             "Search (acquisition source, R14) - and says which of the two traffic_source vocabularies it used"),

        case("C06", "S03", "最近三个月流水多少，客单价呢？", ["vocab_gap", "question_variation"],
             all_of(quotes(value=l3["headline"]["revenue"], aov=(l3["headline"]["aov"], 0.02)),
                    not_number(wrong["aov_last_3_months_as_mean_of_monthly"], abs_tol=0.02)),
             f"流水 = gross revenue {l3['headline']['revenue']:,.0f}; 客单价 = AOV {l3['headline']['aov']:.2f} "
             f"(total/total, R6), not the mean of monthly AOVs {wrong['aov_last_3_months_as_mean_of_monthly']}"),

        case("C07", "S03", "9月到现在收入怎么样？", ["truncated_data", "question_variation"],
             all_of(says_any("没有数据", "无数据", "不完整", "尚未", "还没有", "no data", "not complete", "incomplete",
                             "not yet", "no orders"),
                    not_number(17008.73)),
             "today is 2026-09-01: no September data; last complete month is 2026-08. "
             "17,009 is the item-month trap (R2), not September revenue"),

        case("C08", "S03", "8月收入多少？", ["fanout_trap", "fixed_question"],
             all_of(quotes(value=lm3["headline"]["revenue"]),
                    not_number(wrong["revenue_last_month_fanout"]),
                    not_number(wrong["revenue_last_month_by_item_month"])),
             f"{lm3['headline']['revenue']:,.0f}; known wrong answers: fanout {wrong['revenue_last_month_fanout']:,.0f}, "
             f"item month {wrong['revenue_last_month_by_item_month']:,.0f}, "
             f"net {wrong['revenue_last_month_net_instead_of_gross']:,.0f}"),
    ]


CASES = build_cases()


def diagnose(case: DbCase, result: AgentResult) -> str | None:
    """Which KNOWN mistake did a failing answer make, if any? (truth.known_wrong_answers)"""
    wrong = _truth(Path(case.truth).stem)["known_wrong_answers"]
    hits = [name for name, v in wrong.items()                       # tight: 84.83 must not match 84.78
            if has_number(result.output, v, rel=0.001 if v >= 1000 else 0.0, abs_tol=0.0 if v >= 1000 else 0.005)]
    return ", ".join(hits) or None


# ---------------------------------------------------------------- is the grader right?

def oracle_answers() -> dict[str, tuple[str, list[str]]]:
    """Per case: one reference answer that MUST pass, and answers that MUST fail
    (each fails for one named reason). Run with --oracle whenever a grader changes."""
    s1, s2, s3 = _truth("S01"), _truth("S02"), _truth("S03")
    l3 = s3["last_3_months"]["headline"]

    def j(**kw):
        return json.dumps(kw, ensure_ascii=False)

    return {
        "C01": ("8月收入 573,578，环比 +32.9%，z 3.63 属于异常。唯一的 driver 是 Facebook 获客渠道。\n"
                + j(period=["2026-08"], measure="revenue", value=573578.42, change_pct=0.3289, unusual=True,
                    drivers=[{"dimension": "acq_source", "segment": "Facebook", "measure": "revenue"}],
                    assumptions=[], answer="..."),
                [  # wrong: extra driver
                    "8月收入 573,578（+32.9%）。" + j(period=["2026-08"], value=573578.42, change_pct=0.3289, unusual=True,
                    drivers=[{"dimension": "acq_source", "segment": "Facebook"}, {"dimension": "country", "segment": "China"}]),
                    # wrong: number off by 2%
                    "8月收入 585,000（+32.9%）。" + j(period=["2026-08"], value=585000, change_pct=0.3289, unusual=True,
                    drivers=[{"dimension": "acq_source", "segment": "Facebook"}]),
                    # wrong: no JSON at all
                    "8月收入 573,578，+32.9%，异常，driver 是 Facebook。",
                ]),
        "C02": ("8 月 gross 收入 519,370，+20.3%，属正常增长。但退货率 15.8%，历史每月约 10%；按配送中心拆开，"
                "Chicago IL 退货率 41%，其它 ≤ 13%，7 月已开始。\n"
                + j(period=["2026-08"], measure="revenue", value=519369.9, change_pct=0.2034, unusual=False,
                    drivers=[{"dimension": "dist_center", "segment": "Chicago IL", "measure": "returned_share"}],
                    assumptions=[], answer="..."),
                [  # wrong: never mentions returns
                    "8 月收入 519,370，+20.3%，正常。" + j(period=["2026-08"], value=519369.9, unusual=False, drivers=[]),
                    # wrong: blames a different centre
                    "退货率 15.8%，Philadelphia 有问题。" + j(value=519369.9, drivers=[{"dimension": "dist_center", "segment": "Philadelphia PA"}]),
                ]),
        "C03": ("8月收入 519,370，环比 +20.3%，z 1.99，在正常波动范围内，没有单一 driver。\n"
                + j(period=["2026-08"], measure="revenue", value=519369.9, change_pct=0.2034, unusual=False,
                    drivers=[], assumptions=[], answer="..."),
                [  # wrong: invented cause
                    "8月收入 519,370，+20.3%，主要因为 8 月促销。" + j(value=519369.9, change_pct=0.2034, unusual=False, drivers=[]),
                    # wrong: calls it unusual
                    "8月收入 519,370，+20.3%，异常增长。" + j(value=519369.9, change_pct=0.2034, unusual=True, drivers=[]),
                ]),
        "C04": (f"6–8 月收入 {l3['revenue']:,.0f}，比 3–5 月 +32.1%；只有 8 月异常（z 2.06），没有 driver。\n"
                + j(period=["2026-06", "2026-07", "2026-08"], measure="revenue", value=l3["revenue"], change_pct=0.3205,
                    unusual=True, drivers=[], assumptions=[], answer="..."),
                [  # wrong: answered about one month
                    "8 月收入 519,370，+20.3%。" + j(period=["2026-08"], value=519369.9, change_pct=0.2034, unusual=False, drivers=[]),
                ]),
        "C05": ("按获客渠道（users.traffic_source）看，Search 涨得最多，+59,812。注意会话来源（sessions.traffic_source）"
                "是另一套词汇，如果你指的是那个请告诉我。\n" + j(period=["2026-08"], drivers=[], answer="..."),
                [  # wrong: no disclosure of which vocabulary
                    "Search 涨得最多。" + j(period=["2026-08"], drivers=[]),
                ]),
        "C06": (f"最近三个月流水（gross revenue）{l3['revenue']:,.0f}，客单价（AOV）{l3['aov']:.2f}。\n"
                + j(period=["2026-06", "2026-07", "2026-08"], measure="revenue", value=l3["revenue"], aov=round(l3["aov"], 2),
                    drivers=[], answer="..."),
                [  # wrong: mean of monthly AOVs (R6)
                    f"流水 {l3['revenue']:,.0f}，客单价 84.83。" + j(value=l3["revenue"], aov=84.83, drivers=[]),
                ]),
        "C07": ("今天是 2026-09-01，9 月还没有数据；最后一个完整月是 8 月，收入 519,370。\n"
                + j(period=["2026-09"], measure="revenue", value=None, change_pct=None, unusual=None, drivers=[], answer="..."),
                [  # wrong: item-month trap number presented as September
                    "9 月到目前收入 17,009。" + j(period=["2026-09"], value=17008.73, drivers=[]),
                ]),
        "C08": ("8 月收入 519,370。\n" + j(period=["2026-08"], measure="revenue", value=519369.9, drivers=[], answer="..."),
                [  # wrong: fan-out double count
                    "8 月收入 993,142。" + j(period=["2026-08"], value=993142.21, drivers=[]),
                    # wrong: item month
                    "8 月收入 516,172。" + j(period=["2026-08"], value=516172.14, drivers=[]),
                ]),
    }


def check_graders() -> bool:
    ok = True
    by_id = {c.id: c for c in CASES}
    for cid, (good, bads) in oracle_answers().items():
        c = by_id[cid]
        if not c.grader(c, AgentResult(output=good)):
            print(f"FAIL {cid}: the reference answer does not pass its own grader")
            ok = False
        for i, bad in enumerate(bads):
            r = AgentResult(output=bad)
            if c.grader(c, r):
                print(f"FAIL {cid}: wrong answer #{i} passes: {bad[:80]}")
                ok = False
            else:
                d = diagnose(c, r)
                print(f"      {cid} wrong answer #{i} rejected" + (f"  (known mistake: {d})" if d else ""))
    print("graders OK" if ok else "graders BROKEN")
    return ok


# ---------------------------------------------------------------- running

def run_cases(agent_factory: Callable[[Path], Callable[[str], AgentResult]], cases: list[DbCase] = CASES,
              n_runs: int = 3, name: str = "agent") -> Report:
    """Build one agent per scenario db, run its cases through the harness, merge."""
    outcomes = []
    for db in sorted({c.db for c in cases}):
        agent_fn = agent_factory(ROOT / db)
        rep = run_eval(agent_fn, [c for c in cases if c.db == db], grader=lambda c, r: False,
                       n_runs=n_runs, name=name)
        outcomes += rep.outcomes
    return Report(name=name, outcomes=outcomes, n_runs=n_runs)


def script_factory(thorough: bool):
    """The script baseline as a competitor. It ignores the question by construction."""
    sys.path.insert(0, str(ROOT / "baselines"))
    from script import run as script_run   # noqa: E402

    def factory(db: Path):
        answer = json.dumps(script_run(db, thorough), ensure_ascii=False)
        return lambda question: AgentResult(output=answer, tool_calls=["script"])
    return factory


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")        # the questions are Chinese; the console is cp1252
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--script", action="store_true", help="grade baselines/script.py")
    ap.add_argument("--thorough", action="store_true")
    ap.add_argument("--oracle", action="store_true", help="check the graders against reference and wrong answers")
    a = ap.parse_args()
    if a.oracle:
        sys.exit(0 if check_graders() else 1)
    if a.show or not a.script:
        for c in CASES:
            print(f"{c.id}  [{', '.join(c.tags)}]  {c.db}\n     Q: {c.input}\n     expect: {c.expected}\n")
    if a.script:
        name = "script-thorough" if a.thorough else "script-naive"
        rep = run_cases(script_factory(a.thorough), n_runs=1, name=name)
        print(rep.render())
        print("failures:")
        print(rep.failures(CASES, limit=10, width=160))
        for o in rep.outcomes:
            if not o.passed:
                d = diagnose(next(c for c in CASES if c.id == o.case_id), o.result)
                if d:
                    print(f"  {o.case_id}: known mistake -> {d}")


if __name__ == "__main__":
    main()
