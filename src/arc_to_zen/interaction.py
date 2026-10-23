"""Numbered migration choices and explicitly authorized browser shutdown."""

import subprocess
import time
from pathlib import Path

from arc_to_zen.session import ProfileBusyError, closed_profile


def choose_many(label: str, items: list[str], default: list[int]) -> list[int]:
    if not items:
        raise ValueError(f"No {label.lower()} are available")
    for index, item in enumerate(items, 1):
        print(f"  {index}. {item}")
    suggested = ",".join(str(index + 1) for index in default)
    while True:
        answer = input(f"{label}: numbers separated by commas, or all [{suggested}]: ").strip().lower()
        if not answer and default:
            return default[:]
        if answer == "all":
            return list(range(len(items)))
        try:
            selected = [int(part.strip()) - 1 for part in answer.split(",")]
            if selected and len(set(selected)) == len(selected) and all(0 <= i < len(items) for i in selected):
                return selected
        except ValueError:
            pass
        print("Choose one or more distinct numbers from the list.")


def choose_one(label: str, items: list[tuple[str, str]], default: str) -> str:
    for index, (_key, description) in enumerate(items, 1):
        print(f"  {index}. {description}")
    suggested = next(i for i, (key, _) in enumerate(items, 1) if key == default)
    while True:
        answer = input(f"{label} [{suggested}]: ").strip().lower()
        if not answer:
            return default
        for index, (key, _) in enumerate(items, 1):
            if answer in (str(index), key.lower()):
                return key
        print("Choose a number from the list.")


def features(args) -> None:
    defaults = []
    if not args.boosts_only:
        if not args.no_pinned and not args.appearance_only:
            defaults.append(0)
        if not args.no_unpinned and not args.appearance_only:
            defaults.append(1)
        if args.favorites != "skip" and not args.appearance_only:
            defaults.append(2)
        if not args.no_appearance:
            defaults.append(3)
    if args.boosts:
        defaults.append(4)
    print("Choose what to import. Unselected features are left untouched in Zen.")
    selected = set(
        choose_many(
            "Features",
            [
                "Pinned tabs and nested folders",
                "Unpinned tabs",
                "Favorites",
                "Space colors and icons",
                "Site Boosts (all workspaces in the selected Zen profile)",
            ],
            defaults,
        )
    )
    args.no_pinned = 0 not in selected
    args.no_unpinned = 1 not in selected
    if 2 in selected:
        args.favorites = choose_one(
            "Favorites",
            [
                ("essentials", "Container-specific Essentials"),
                ("folder", "A Favorites folder in each workspace"),
            ],
            args.favorites if args.favorites != "skip" else "essentials",
        )
    else:
        args.favorites = "skip"
    args.no_appearance = 3 not in selected
    args.boosts = 4 in selected
    args.boosts_only = selected == {4}
    args.appearance_only = not (selected & {0, 1, 2}) and 3 in selected
    args.mode = choose_one(
        "Existing imported items",
        [
            ("add", "Add missing items and keep edits"),
            ("update", "Update imported items from Arc (can overwrite edits)"),
            ("skip-existing", "Skip populated destination workspaces"),
        ],
        args.mode,
    )
    if not args.boosts_only:
        creation = choose_one(
            "Missing workspaces and containers",
            [
                ("create", "Create missing destinations"),
                ("existing", "Require existing destinations"),
            ],
            "existing" if args.no_create else "create",
        )
        args.no_create = creation == "existing"
    if args.boosts:
        print("JavaScript is never transferred or executed.")
        partial = choose_one(
            "Unsupported Boost settings",
            [
                ("skip", "Skip Boosts with unsupported settings"),
                ("partial", "Import supported parts and report omissions"),
            ],
            "partial" if args.allow_partial_boosts else "skip",
        )
        args.allow_partial_boosts = partial == "partial"


def quit_zen() -> bool:
    """Ask macOS to quit normally, without signals or force quit."""
    script = (
        'if application id "app.zen-browser.zen" is running then\n'
        'tell application id "app.zen-browser.zen" to quit\nend if'
    )
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def prepare_browser(profile: Path) -> bool:
    """Keep the user's choices while waiting for the browser to release its files."""
    while True:
        try:
            with closed_profile(profile):
                return True
        except ProfileBusyError as error:
            print(error)
        answer = (
            input(
                "Close Zen yourself and press Enter to check again; Y asks Zen to quit all windows; C cancels [Enter]: "
            )
            .strip()
            .lower()
        )
        if answer in ("c", "cancel", "n", "no"):
            return False
        if answer in ("y", "yes"):
            if not quit_zen():
                print("Zen could not be closed automatically. Close it manually or cancel.")
                continue
            print("Waiting for Zen to finish saving and release its profile...")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with closed_profile(profile):
                        return True
                except ProfileBusyError:
                    time.sleep(0.25)
            print("Zen has not released the profile yet. Resolve any open dialogs, or close it manually.")
        elif answer:
            print("Choose Enter, Y, or C.")
