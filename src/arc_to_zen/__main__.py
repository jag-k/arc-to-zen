"""Module and console entry point."""

import sys

from arc_to_zen.cli import main as cli_main


def main() -> None:
    try:
        cli_main()
    except (ValueError, KeyError, IndexError, TypeError, OSError, StopIteration) as error:
        sys.exit(f"Error: {error}")
    except (KeyboardInterrupt, EOFError):
        sys.exit("Cancelled. No further changes made.")


if __name__ == "__main__":
    main()
