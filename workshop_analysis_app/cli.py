"""Command-line argument parsing and process entrypoint."""

import argparse
import sys

from .app import WorkshopAnalysis
from .common import MIN_PYTHON, SCRIPT_ROOT


def build_parser():
    parser = argparse.ArgumentParser(
        prog="WorkshopAnalysis",
        description="Open the WorkshopAnalysis command interpreter or run one command.",
    )
    parser.add_argument(
        "-StateRoot",
        "--state-root",
        dest="state_root",
        default=str(SCRIPT_ROOT / "state"),
        help="Directory for config, database, downloaded workshop content, and tools.",
    )
    parser.add_argument(
        "--no-tool-bootstrap",
        dest="no_tool_bootstrap",
        action="store_true",
        help="Skip Source 2 / UE5 tool installation checks for download commands.",
    )
    parser.add_argument(
        "--raw",
        "-Raw",
        dest="raw_mode",
        action="store_true",
        help="Use the line-oriented text interpreter instead of the in-place terminal UI.",
    )
    parser.add_argument(
        "--debug",
        "--verbose",
        dest="debug",
        action="store_true",
        help="Show debug details, including raw SteamCMD output.",
    )
    parser.add_argument(
        "-Bootstrap",
        "--bootstrap",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-Reconfigure",
        "--reconfigure",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-NoToolBootstrap",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-ManageCatalog",
        "--manage-catalog",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "commands",
        nargs="*",
        help="Optional command to run once, such as bootstrap, download, catalog, status, or help.",
    )
    return parser

def main(argv=None):
    if sys.version_info < MIN_PYTHON:
        print(
            "ERROR: Python {0}.{1}+ is required. Run .\\setup.ps1 to install it.".format(
                MIN_PYTHON[0], MIN_PYTHON[1]
            ),
            file=sys.stderr,
        )
        return 1

    parser = build_parser()
    args, command_options = parser.parse_known_args(argv)
    if command_options:
        args.commands.extend(command_options)
    app = WorkshopAnalysis(
        args.state_root,
        no_tool_bootstrap=args.no_tool_bootstrap or args.NoToolBootstrap,
        debug=args.debug,
    )
    try:
        app.run(
            commands=args.commands,
            bootstrap=args.bootstrap,
            reconfigure=args.reconfigure,
            manage_catalog=args.manage_catalog,
            raw_mode=args.raw_mode,
        )
    except KeyboardInterrupt:
        print()
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print("ERROR: {0}".format(exc), file=sys.stderr)
        return 1
    return 0
