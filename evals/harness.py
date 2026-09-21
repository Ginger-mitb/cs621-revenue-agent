# Copied unchanged from session4/week4_stu/agentlab/evals.py (the Week 3 harness) on 2026-09-20,
# so the project is self-contained for team members who do not have the lab folders.

"""Evaluation harness. Week 3 is built on this; every later week reports through it.

Design commitments, all of which are teaching points:
  - n_runs defaults to 3. A single run is not a measurement.
  - Every report carries a baseline. A pass rate with nothing to compare it to
    is decoration.
  - Failures are clustered by tag, not listed individually.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable

Grader = Callable[["Case", "AgentResult"], bool]


@dataclass
class Case:
    id: str
    input: str
    expected: Any = None
    tags: list[str] = field(default_factory=list)
    # Optional per-case grader, overriding the one passed to run_eval.
    # Needed because one grader cannot fit every case: "list every deadline"
    # must require ALL of them, while "what is the error rate" needs any one
    # acceptable phrasing.
    grader: Any = None


@dataclass
class AgentResult:
    output: str
    steps: int = 0
    tool_calls: list[str] = field(default_factory=list)
    # what each call was actually asked for. Names tell you the agent
    # searched; only the arguments tell you what it searched FOR.
    tool_args: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str | None = None


@dataclass
class CaseOutcome:
    case_id: str
    passed: bool
    run_index: int
    result: AgentResult
    tags: list[str] = field(default_factory=list)


@dataclass
class Report:
    name: str
    outcomes: list[CaseOutcome]
    n_runs: int

    @property
    def pass_rates(self) -> list[float]:
        by_run: dict[int, list[bool]] = {}
        for o in self.outcomes:
            by_run.setdefault(o.run_index, []).append(o.passed)
        return [sum(v) / len(v) for _, v in sorted(by_run.items())]

    @property
    def mean_pass(self) -> float:
        return statistics.fmean(self.pass_rates) if self.pass_rates else 0.0

    @property
    def stdev_pass(self) -> float:
        rs = self.pass_rates
        return statistics.stdev(rs) if len(rs) > 1 else 0.0

    @property
    def cost_per_task(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(o.result.cost_usd for o in self.outcomes) / len(self.outcomes)

    @property
    def tokens_per_task(self) -> float:
        """Input + output tokens, averaged over every execution.

        Week 4 turns on this number: stuffing the corpus into the prompt is
        paid on EVERY turn of the loop, not once.
        """
        if not self.outcomes:
            return 0.0
        return statistics.fmean(o.result.input_tokens + o.result.output_tokens
                                for o in self.outcomes)

    @property
    def mean_latency(self) -> float:
        if not self.outcomes:
            return 0.0
        return statistics.fmean(o.result.latency_s for o in self.outcomes)

    def flaky_cases(self) -> list[str]:
        """Cases that passed on some runs and failed on others. Usually the
        most informative failures in the whole report."""
        by_case: dict[str, set[bool]] = {}
        for o in self.outcomes:
            by_case.setdefault(o.case_id, set()).add(o.passed)
        return sorted(cid for cid, s in by_case.items() if len(s) > 1)

    def failures(self, cases: list["Case"] | None = None,
                 limit: int = 4, width: int = 220) -> str:
        """The actual output of failing cases.

        A count tells you a case failed. It does not tell you whether the
        agent was wrong or your grader was too strict - and those need
        opposite fixes. Read the text before you change anything.
        """
        by_id = {c.id: c for c in (cases or [])}
        seen, lines = set(), []
        for o in self.outcomes:
            if o.passed or o.case_id in seen:
                continue
            seen.add(o.case_id)
            c = by_id.get(o.case_id)
            if c is not None:
                exp = c.expected
                exp = exp if isinstance(exp, (list, tuple)) else [exp]
                lines.append(f"  {o.case_id}  {c.input[:100]}")
                lines.append(f"      wanted any of: {list(exp)[:5]}")
            else:
                lines.append(f"  {o.case_id}")
            if o.result.error:
                lines.append(f"      RAISED: {o.result.error[:width]}")
            else:
                out = " ".join(o.result.output.split())
                lines.append(f"      got:  {out[:width] or '(empty)'}")
                if o.result.tool_args:
                    lines.append("      calls:")
                    for c in o.result.tool_args[:8]:
                        lines.append(f"        {c}")
                elif o.result.tool_calls:
                    lines.append(f"      tools: {' > '.join(o.result.tool_calls)}")
            lines.append("")
            if len(seen) >= limit:
                break
        if not lines:
            return "  (nothing failed)"
        return "\n".join(lines)

    def clusters(self) -> dict[str, tuple[int, int]]:
        """tag -> (cases that failed, cases with this tag).

        One verdict per CASE, not per execution: a case counts as failed if it
        failed on any run. Denominators are therefore case counts, which is
        what a reader expects. Cases that passed some runs and failed others
        are listed separately by flaky_cases().
        """
        verdict: dict[str, bool] = {}          # case_id -> passed every run
        tags: dict[str, list[str]] = {}
        for o in self.outcomes:
            verdict[o.case_id] = verdict.get(o.case_id, True) and o.passed
            tags[o.case_id] = o.tags or ["untagged"]

        agg: dict[str, list[int]] = {}
        for case_id, passed in verdict.items():
            for t in tags[case_id]:
                a = agg.setdefault(t, [0, 0])
                a[1] += 1
                if not passed:
                    a[0] += 1
        return {t: (f, n) for t, (f, n) in agg.items()}

    def render(self, baseline: "Report | None" = None) -> str:
        lines = [
            f"=== {self.name} ===",
            f"pass rate     {self.mean_pass:.1%}  (sd {self.stdev_pass:.1%} over {self.n_runs} runs)",
            f"cost/task     ${self.cost_per_task:.4f}",
            f"tokens/task   {self.tokens_per_task:,.0f}",
            f"latency/task  {self.mean_latency:.2f}s",
        ]
        if baseline:
            delta = self.mean_pass - baseline.mean_pass
            cost_x = (self.cost_per_task / baseline.cost_per_task
                      if baseline.cost_per_task else float("inf"))
            lines += [
                f"vs {baseline.name}: {delta:+.1%} pass rate at {cost_x:.1f}x cost",
            ]
            if delta <= self.stdev_pass:
                lines.append("  NOTE: improvement is within one standard deviation. "
                             "You have not shown an improvement.")
        flaky = self.flaky_cases()
        if flaky:
            lines.append(f"flaky cases   {', '.join(flaky)}")
        lines.append("failure clusters:")
        for tag, (f, n) in sorted(self.clusters().items(), key=lambda kv: -kv[1][0]):
            if f:
                lines.append(f"  {tag:<24} {f}/{n} cases failed")
        return "\n".join(lines)


def run_eval(agent_fn: Callable[[str], AgentResult], cases: list[Case],
             grader: Grader, n_runs: int = 3, name: str = "agent") -> Report:
    outcomes: list[CaseOutcome] = []
    for run_index in range(n_runs):
        for case in cases:
            try:
                result = agent_fn(case.input)
            except Exception as e:
                result = AgentResult(output="", error=f"{type(e).__name__}: {e}")
            g = case.grader or grader          # per-case override
            passed = False if result.error else bool(g(case, result))
            tags = list(case.tags) + (["crash"] if result.error else [])
            outcomes.append(CaseOutcome(case.id, passed, run_index, result, tags))
    return Report(name=name, outcomes=outcomes, n_runs=n_runs)


# ------------------------------------------------------------------ graders

def exact_match(case: Case, result: AgentResult) -> bool:
    return str(case.expected).strip().lower() == result.output.strip().lower()


def _norm(t: str) -> str:
    """Normalise before matching.

    LLMs format numbers for humans: "3,600 ms", "2,400 seconds", and often with
    narrow no-break spaces. A grader that misses those is measuring formatting,
    not correctness.
    """
    t = t.lower()
    for ch, rep in (("\u202f", " "), ("\u00a0", " "),      # thin / nb space
                    ("\u2011", "-"), ("\u2013", "-"),      # nb hyphen, en dash
                    ("\u2019", "'"), ("\u2018", "'"),      # curly apostrophes
                    ("\u201c", '"'), ("\u201d", '"')):     # curly quotes
        t = t.replace(ch, rep)
    t = re.sub(r"(?<=\d),(?=\d)", "", t)                    # 3,600 -> 3600
    t = re.sub(r"(?<=\d)\s(?=\d{3}\b)", "", t)              # 3 600 -> 3600
    return re.sub(r"\s+", " ", t)


def contains(case: Case, result: AgentResult) -> bool:
    return _norm(str(case.expected)) in _norm(result.output)


def any_of(*expected: str) -> Grader:
    """Pass if ANY of these appears. For answers with several valid phrasings."""
    def _g(case: Case, result: AgentResult) -> bool:
        out = _norm(result.output)
        return any(_norm(e) in out for e in expected)
    return _g


def expected_any(case: Case, result: AgentResult) -> bool:
    """Like contains, but case.expected may be a list of acceptable strings."""
    exp = case.expected if isinstance(case.expected, (list, tuple)) else [case.expected]
    out = _norm(result.output)
    return any(_norm(str(e)) in out for e in exp)


def used_tool(tool_name: str) -> Grader:
    """Trajectory assertion: did it get there the right way?"""
    def _g(case: Case, result: AgentResult) -> bool:
        return tool_name in result.tool_calls
    return _g


def all_present(case: Case, result: AgentResult) -> bool:
    """Pass only if EVERY expected item appears.

    For questions of the form "list every X" or "which ones did Y". Grading
    those with expected_any means mentioning one of five passes, which is not
    what the question asked.

    Each expected item may itself be a list of acceptable phrasings:
        expected=[["18:00", "6pm", "2 hours"], ["02:00", "2am"], "60 minutes"]
    means all three deadlines must appear, each in any of its forms.
    """
    exp = case.expected
    if not isinstance(exp, (list, tuple)):
        exp = [exp]
    out = _norm(result.output)
    for item in exp:
        forms = item if isinstance(item, (list, tuple)) else [item]
        if not any(_norm(str(f)) in out for f in forms):
            return False
    return True


def all_of(*graders: Grader) -> Grader:
    def _g(case: Case, result: AgentResult) -> bool:
        return all(g(case, result) for g in graders)
    return _g


RUBRIC_PROMPT = """You are grading an AI agent's answer.

Question: {question}
Reference answer: {expected}
Agent answer: {actual}

Does the agent answer convey the same substance as the reference? Ignore style,
length, and formatting. Reply with exactly one word: PASS or FAIL."""


def rubric_judge(llm) -> Grader:
    """LLM-as-judge. Biased — position, verbosity, self-preference. Use it only
    where a deterministic check genuinely cannot work, and say so in the report."""
    def _g(case: Case, result: AgentResult) -> bool:
        prompt = RUBRIC_PROMPT.format(question=case.input, expected=case.expected,
                                      actual=result.output)
        resp = llm.chat([{"role": "user", "content": prompt}], max_tokens=8)
        return resp.text.strip().upper().startswith("PASS")
    return _g
