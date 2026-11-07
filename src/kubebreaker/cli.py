"""kubebreaker command line."""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from kubebreaker.chaos import CATALOG, runner
from kubebreaker.kube import Kube


def cmd_faults(_args) -> int:
    for f in CATALOG.values():
        print(f"{f.id:<20} {f.tier:<7} {f.title}")
    return 0


def _fault(fault_id: str):
    try:
        return CATALOG[fault_id]
    except KeyError:
        sys.exit(f"unknown fault {fault_id!r}. Run 'kubebreaker faults' to list them.")


def cmd_reset(_args) -> int:
    runner.reset_app(Kube.from_env())
    print("baseline OK")
    return 0


def cmd_inject(args) -> int:
    fault = _fault(args.fault)
    k = Kube.from_env()
    if not args.no_reset:
        print("resetting app...")
        runner.reset_app(k)
    print(f"injecting {fault.id}...")
    seconds = runner.inject(k, fault)
    print(f"symptom visible after {seconds:.0f}s")
    print(f"alert: {fault.alert}")
    return 0


def cmd_verify(args) -> int:
    fault = _fault(args.fault)
    problems = runner.verify(Kube.from_env(), fault, timeout=args.timeout)
    if problems:
        print("NOT fixed:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("fixed: app is back to baseline")
    return 0


def cmd_selftest(args) -> int:
    k = Kube.from_env()
    faults = [_fault(i) for i in args.faults] if args.faults else list(CATALOG.values())
    failed = 0
    for fault in faults:
        r = runner.selftest(k, fault)
        status = "PASS" if r.ok else "FAIL"
        print(
            f"{status}  {fault.id:<20} symptom {r.symptom_seconds:5.1f}s  "
            f"recover {r.recover_seconds:5.1f}s  {r.error}",
            flush=True,
        )
        for p in r.problems:
            print(f"        - {p}")
        failed += not r.ok
    runner.reset_app(k)
    print(f"\n{len(faults) - failed}/{len(faults)} faults passed")
    return 1 if failed else 0


def cmd_run(args) -> int:
    from kubebreaker.episode import run_episode
    from kubebreaker.llm import make_client

    fault = _fault(args.fault)
    llm = make_client(args.provider, args.model)
    print(f"episode: {fault.id} with {llm.provider}/{llm.model} (max {args.max_steps} steps)")
    r = run_episode(Kube.from_env(), fault, llm, args.max_steps, args.max_tokens)
    verdict = "RESOLVED" if r.resolved else "NOT RESOLVED"
    ttr = f"{r.time_to_resolve_seconds:.0f}s" if r.time_to_resolve_seconds else "-"
    print(f"\n{verdict}  steps={r.steps}  time={ttr}  stop={r.stop_reason}")
    print(f"tokens: in={r.input_tokens} (cached {r.cached_tokens}) out={r.output_tokens}")
    print(f"agent's root cause: {r.root_cause or '-'}")
    print(f"agent's fix:        {r.fix_summary or '-'}")
    print(f"ground truth:       {r.ground_truth}")
    for p in r.remaining_problems:
        print(f"  remaining: {p}")
    if r.error:
        print(f"error: {r.error}")
    print(f"trace: {r.run_dir}/trace.jsonl")
    return 0 if r.resolved else 1


def main() -> None:
    load_dotenv(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(prog="kubebreaker")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("faults", help="list the fault catalog").set_defaults(func=cmd_faults)
    sub.add_parser("reset", help="reset the shop app to baseline").set_defaults(func=cmd_reset)

    p = sub.add_parser("inject", help="reset, inject a fault and wait for its symptom")
    p.add_argument("fault")
    p.add_argument("--no-reset", action="store_true", help="inject without resetting first")
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("verify", help="check whether the app has recovered from a fault")
    p.add_argument("fault")
    p.add_argument("--timeout", type=float, default=10)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("selftest", help="inject each fault, apply its reference fix, verify")
    p.add_argument("faults", nargs="*", help="fault ids (default: all)")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("run", help="run one agent episode against a fault")
    p.add_argument("fault")
    p.add_argument("--provider", help="openai or anthropic (default: LLM_PROVIDER)")
    p.add_argument("--model", help="model name (default: LLM_MODEL)")
    p.add_argument("--max-steps", type=int, default=20)
    p.add_argument("--max-tokens", type=int, default=600_000)
    p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
