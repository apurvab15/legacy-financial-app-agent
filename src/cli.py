"""CLI. Replay (no LLM key). Discovery: python -m src discover --goal ... --member-id 12345"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

from src.guardrails import PolicyDenied, assert_action_allowed, load_policy
from src.mock_server import ensure_mock
from src.redact import RedactFilter
from src.replay import load_capability, run_replay
from src.result import ReplayResult

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CAPABILITY = REPO_ROOT / "capabilities" / "lookup-savings-balance.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_POLICY = REPO_ROOT / "policies" / "allowlist.yaml"
DEFAULT_DISCOVERED = REPO_ROOT / "capabilities" / "lookup-savings-balance.discovered.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay a capability (no LLM) or discover one from a goal (LLM)."
    )
    parser.add_argument(
        "--member-id",
        help="Bound to input_schema.member_id (e.g. 12345 or 99999)",
    )
    parser.add_argument(
        "--capability",
        type=Path,
        default=DEFAULT_CAPABILITY,
        help="Path to capability JSON (replay)",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--entry",
        help="Override capability app.entry (path-allowlist demo)",
    )
    parser.add_argument(
        "--demo-action",
        help="Check a single action against the allowlist (e.g. download) and exit",
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--headed", action="store_true", help="Show the browser")
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument(
        "--goal",
        help="If set (or subcommand discover), run LLM discovery instead of replay",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_DISCOVERED,
        help="Where discovery writes the compiled capability JSON",
    )
    parser.add_argument("--max-steps", type=int, default=20)
    return parser


def _print_result(result: ReplayResult | dict) -> int:
    payload = result.to_dict() if isinstance(result, ReplayResult) else result
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    status = payload.get("status")
    if status in ("success", "business_outcome"):
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger().addFilter(RedactFilter())
    raw = list(argv if argv is not None else sys.argv[1:])
    discover_mode = bool(raw and raw[0] == "discover")
    if discover_mode:
        raw = raw[1:]
    args = build_parser().parse_args(raw)
    policy = load_policy(Path(args.policy))

    if discover_mode or args.goal:
        if not args.goal:
            print("error: discovery requires --goal", file=sys.stderr)
            return 2
        if not args.member_id:
            print("error: discovery requires --member-id", file=sys.stderr)
            return 2
        from src.discover import run_discovery

        payload = run_discovery(
            goal=args.goal,
            member_id=str(args.member_id),
            base_url=args.base_url,
            repo_root=REPO_ROOT,
            headed=args.headed,
            max_steps=args.max_steps,
            output_path=Path(args.output),
            timeout_ms=args.timeout_ms,
        )
        return _print_result(payload)

    if args.demo_action:
        try:
            assert_action_allowed(policy, args.demo_action)
            result = ReplayResult(
                status="failed",
                outcome_code="internal_error",
                expected="denied action for this demo",
                observed=f"{args.demo_action} is allowlisted",
            )
        except PolicyDenied as exc:
            result = ReplayResult(
                status="failed",
                outcome_code=exc.outcome_code,
                expected=exc.expected,
                observed=exc.observed,
            )
        dest = REPO_ROOT / "evidence" / f"replay-{result.outcome_code}"
        dest.mkdir(parents=True, exist_ok=True)
        result.evidence_dir = str(dest.relative_to(REPO_ROOT))
        from src.evidence import write_result

        write_result(dest, result)
        return _print_result(result)

    capability = load_capability(args.capability)
    required = ((capability.get("input_schema") or {}).get("required")) or []
    if "member_id" in required and not args.member_id:
        print("error: --member-id is required for this capability", file=sys.stderr)
        return 2

    host = urlparse(args.base_url).hostname or ""
    if host in {"127.0.0.1", "localhost"}:
        ensure_mock(args.base_url)

    params = {"member_id": str(args.member_id)} if args.member_id is not None else {}
    result = run_replay(
        capability=capability,
        params=params,
        base_url=args.base_url,
        repo_root=REPO_ROOT,
        headed=args.headed,
        timeout_ms=args.timeout_ms,
        entry_override=args.entry,
        policy=policy,
    )
    return _print_result(result)


if __name__ == "__main__":
    raise SystemExit(main())
