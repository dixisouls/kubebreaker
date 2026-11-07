"""LLM judge for root-cause accuracy: compares the agent's write-up with the ground truth."""

import json
import os
import re
from dataclasses import dataclass

from kubebreaker.llm import LLMClient, Message, make_client

JUDGE_PROMPT = """\
You grade root-cause analyses written by an on-call engineer after a Kubernetes incident.
You get the true root cause and the engineer's write-up. Grade only the root cause, not style.

- correct (1.0): names the same object and the same misconfiguration as the truth. Exact values
  are not required if the mechanism is right.
- partial (0.5): the right object or area, but the mechanism is vague or partly wrong, or it
  describes the symptom chain without the underlying change.
- wrong (0.0): a different cause, only symptoms, or empty.

Reply with only a JSON object: {"verdict": "correct|partial|wrong", "reason": "<one sentence>"}
"""

SCORES = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}


@dataclass
class Judgement:
    verdict: str
    score: float
    reason: str
    judge: str  # provider/model, or "none" when no LLM call was needed


def judge_prompt(ground_truth: str, root_cause: str, fix_summary: str) -> str:
    return (
        f"TRUE ROOT CAUSE:\n{ground_truth}\n\n"
        f"ENGINEER'S ROOT CAUSE:\n{root_cause}\n\n"
        f"ENGINEER'S FIX SUMMARY (context only):\n{fix_summary or '-'}"
    )


def parse_verdict(text: str) -> tuple[str, str]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            verdict = str(data.get("verdict", "")).lower()
            if verdict in SCORES:
                return verdict, str(data.get("reason", ""))
        except json.JSONDecodeError:
            pass
    return "wrong", f"unparseable judge reply: {(text or '')[:200]!r}"


def judge_root_cause(
    ground_truth: str, root_cause: str, fix_summary: str, llm: LLMClient | None = None
) -> Judgement:
    if not root_cause.strip():
        return Judgement("wrong", 0.0, "the agent gave no root cause", "none")
    llm = llm or make_client(
        os.environ.get("JUDGE_PROVIDER") or None, os.environ.get("JUDGE_MODEL") or None
    )
    resp = llm.chat(
        JUDGE_PROMPT, [Message("user", judge_prompt(ground_truth, root_cause, fix_summary))], []
    )
    verdict, reason = parse_verdict(resp.message.text or "")
    return Judgement(verdict, SCORES[verdict], reason, f"{llm.provider}/{llm.model}")
