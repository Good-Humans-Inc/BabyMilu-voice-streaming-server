#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from harness.environment import load_environment  # noqa: E402
from harness.preflight import render_preflight, run_preflight, has_failures  # noqa: E402
from harness.reporting import ArtifactWriter  # noqa: E402


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run BabyMilu shared smoke scenarios")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env", default="staging", help="Named smoke environment config")
    common.add_argument(
        "--config",
        help="Optional explicit environment config JSON file. Defaults to tools/smoke/environments/<env>.json or <env>.local.json",
    )

    preflight = subparsers.add_parser("preflight", parents=[common], help="Check auth and local tooling")
    preflight.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on warnings as well as failures",
    )

    subparsers.add_parser("list-scenarios", parents=[common], help="List available scenarios")

    run = subparsers.add_parser("run", parents=[common], help="Run a named smoke scenario")
    run.add_argument("--scenario", required=True, help="Scenario name, e.g. scheduled.reminder")
    run.add_argument("--uid", required=True, help="User UID, usually a phone number")
    run.add_argument("--device-id", help="Device ID to simulate for plushie scenarios")
    run.add_argument("--label", help="Human-readable label for the test document")
    run.add_argument(
        "--repeat",
        choices=("none", "weekly"),
        default="none",
        help="Repeat schedule to create for scheduled scenarios",
    )
    run.add_argument(
        "--channel",
        choices=("app", "plushie", "both"),
        default="both",
        help="Delivery channel for reminder scenarios",
    )
    run.add_argument(
        "--lead-seconds",
        type=int,
        default=5,
        help="How far in the past/future to place nextOccurrenceUTC before triggering the scheduler",
    )
    run.add_argument(
        "--trigger-mode",
        choices=("invoke-function", "wait-only"),
        default="invoke-function",
        help="How to trigger the scheduler after setup",
    )
    run.add_argument(
        "--keep-docs",
        action="store_true",
        help="Keep created Firestore documents after the run",
    )
    run.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip auth/tooling checks before execution",
    )
    run.add_argument(
        "--timeout-seconds",
        type=int,
        default=45,
        help="Overall wait budget for scheduler side effects and websocket capture",
    )
    run.add_argument(
        "--prompt",
        help="Optional free-form prompt for future interaction scenarios",
    )

    return parser


async def handle_run(args: argparse.Namespace) -> int:
    from harness.context import ScenarioContext
    from harness.registry import make_scenario

    env = load_environment(args.env, args.config)
    if not args.skip_preflight:
        checks = run_preflight(env, strict=False)
        print(render_preflight(checks))
        if has_failures(checks):
            return 2

    scenario = make_scenario(args.scenario)
    writer = ArtifactWriter(env.artifact_root)
    artifact_dir = writer.begin_run(args.scenario, args.uid)
    context = ScenarioContext(
        environment=env,
        args=args,
        artifact_writer=writer,
        artifact_dir=artifact_dir,
    )
    result = await scenario.run(context)
    writer.write_json("result.json", result.to_dict(), artifact_dir)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.success else 1


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list-scenarios":
        from harness.registry import list_scenarios

        env = load_environment(args.env, args.config)
        print(f"environment: {env.name}")
        for item in list_scenarios():
            print(f"- {item.name}: {item.description}")
        return 0

    if args.command == "preflight":
        env = load_environment(args.env, args.config)
        checks = run_preflight(env, strict=args.strict)
        print(render_preflight(checks))
        if has_failures(checks):
            return 2
        if args.strict and any(check.level == "warn" for check in checks):
            return 3
        return 0

    if args.command == "run":
        return asyncio.run(handle_run(args))

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
