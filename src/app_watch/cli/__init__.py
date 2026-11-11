"""Command-line entry point for available App-watch functionality."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app_watch import __version__
from app_watch.config import load_config, prepare_config
from app_watch.config.runtime import parse_parameters
from app_watch.errors import BlockedError, ConfigurationError, StateError


class _CommandParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        arguments = list(sys.argv[1:] if args is None else args)
        index = 0
        while index < len(arguments):
            word = arguments[index]
            if word in ("--config", "--param"):
                index += 2
            elif word == "--version" or word.startswith(("--config=", "--param=")):
                index += 1
            else:
                break
        if index < len(arguments) and arguments[index] == "notify":
            parsed = super().parse_args(arguments[: index + 1], namespace)
            parsed.arguments = arguments[index + 1 :]
            if any("\0" in value for value in parsed.arguments):
                self.error("Notification arguments must not contain NUL characters")
            return parsed
        return super().parse_args(arguments, namespace)


def create_parser() -> argparse.ArgumentParser:
    parser = _CommandParser(
        prog="app-watch",
        description="Application shutdown watches for the current account (systemd user services)",
    )
    parser.add_argument("--version", action="version", version=f"app-watch {__version__}")
    parser.add_argument("--config", type=Path, help="one watch YAML file")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate watch configuration")
    validate.add_argument(
        "--schema-only", action="store_true", help="skip account/filesystem checks"
    )
    status = commands.add_parser("status", help="show watch health and action recovery state")
    status.add_argument("--json", action="store_true", help="output status as JSON")
    status.add_argument("--verbose", action="store_true", help="include recovery details")
    commands.add_parser("list", help="list configured watches and installed monitors")
    commands.add_parser(
        "notify", add_help=False, help="pass arguments to the configured external notifier"
    )
    monitor = commands.add_parser("monitor", help="monitor one watch in the foreground")
    monitor.add_argument("--expected-identity", help=argparse.SUPPRESS)
    worker = commands.add_parser("exec-worker", help="internal worker service entry point")
    worker.add_argument("selection")
    for action in ("install", "update", "disable", "remove"):
        help_text = f"{action} this watch's user monitor service"
        commands.add_parser(action, help=help_text, description=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "exec-worker":
            from app_watch.cli.monitor import run_worker

            if args.config is not None or args.param:
                parser.error("Internal workers do not read live configuration or parameters")
            return run_worker(args.selection)
        params = parse_parameters(args.param)
        if args.command in ("status", "list"):
            from app_watch.cli.status import inventory, print_inventory, print_status, watch_status

            if args.config is None:
                if args.command != "list" or params:
                    parser.error("--config is required for status or explicit --param")
                print_inventory(inventory())
            else:
                result = watch_status(args.config, runtime_params=params)
                if args.command == "list":
                    print_inventory([result])
                elif args.json:
                    if not args.verbose:
                        result.pop("details")
                    print(json.dumps(result, sort_keys=True))
                else:
                    print_status(result, verbose=args.verbose)
            return 0
        if args.config is None:
            parser.error("--config is required for this command")
        if args.command in ("disable", "remove"):
            from app_watch.services.monitor import manage_service

            if params:
                parser.error("Disable/remove recover the installed selection; omit --param")
            manage_service(args.config, args.command)
            print(
                f"Monitor service: {args.command.upper()}D"
                if args.command == "remove"
                else "Monitor service: DISABLED"
            )
            return 0
        config = load_config(args.config)
        if args.command == "notify":
            from app_watch.notifications.external import notify

            if config.external_notifier is None:
                raise ConfigurationError("external_notifier is required by app-watch notify")
            logging.basicConfig(level=config.logging.level.upper())
            notify(config.external_notifier, args.arguments)
            return 0
        if args.command in ("monitor", "install", "update"):
            watch = prepare_config(config, args.config, runtime_params=params)
            if args.command in ("install", "update"):
                from app_watch.services.monitor import manage_service

                manage_service(args.config, args.command, watch=watch)
                print(
                    "Monitor service: INSTALLED"
                    if args.command == "install"
                    else "Monitor service: UPDATED"
                )
                return 0
            from app_watch.cli.monitor import run_monitor

            logging.basicConfig(level=config.logging.level.upper())
            return run_monitor(watch, expected_identity=args.expected_identity)
        if args.schema_only:
            # Schema-only validation intentionally does not resolve paths or templates.
            if params:
                raise ConfigurationError("--param requires validation without --schema-only")
        else:
            watch = prepare_config(config, args.config, runtime_params=params)
            if watch.config.trigger.enabled:
                from app_watch.cli.status import safe_text, watch_status
                from app_watch.services.monitor import RealSystemdController

                report = watch_status(args.config, runtime_params=params)
                diagnostics = list(report["diagnostics"])
                controller = RealSystemdController()
                if not controller.manager_reachable():
                    diagnostics.append("systemd user manager is unavailable")
                elif not controller.template_ready():
                    diagnostics.append("App-watch worker template is unavailable or incompatible")
                healthy = (
                    report["monitor"] == "RUNNING"
                    and report["service_enabled"] is True
                    and report["application"]
                    not in ("UNKNOWN", "AMBIGUOUS", "INSPECTION UNAVAILABLE")
                    and report["dispatch"] != "BLOCKED"
                    and not diagnostics
                )
                if not healthy:
                    print("Monitor service: " + safe_text(report["monitor"]))
                    print("Result: SETUP INCOMPLETE")
                    for diagnostic in diagnostics:
                        print("Diagnostic: " + safe_text(diagnostic))
                    return 2
    except (ConfigurationError, BlockedError, StateError, OSError) as error:
        # Escape control characters before printing operator-supplied diagnostics.
        message = "".join(
            char if char.isprintable() or char == "\n" else ascii(char)[1:-1] for char in str(error)
        )
        category = (
            "Configuration error" if isinstance(error, ConfigurationError) else "App-watch error"
        )
        print(f"{category}: {message}", file=sys.stderr)
        return 2
    print(
        "Result: OK (schema only)"
        if args.schema_only
        else "Result: OK (configuration, paths and monitoring health)"
        if config.trigger.enabled
        else "Result: OK (configuration and paths)"
    )
    return 0
