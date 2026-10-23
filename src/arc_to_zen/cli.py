"""Discovery, preview and command-line wizard for Arc → Zen."""

import argparse
import configparser
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_to_zen import arc_data, interaction
from arc_to_zen.session import (
    SESSION,
    ProfileBusyError,
    ProfileChangedError,
    decode_session,
    encode_session,
    find_profile,
    restore,
)
from arc_to_zen.transactions import commit, json_bytes, rollback_plan
from arc_to_zen.zen_plan import STATE_FILE, TESTED_VERSIONS, build_plan


def zen_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/zen"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "zen"
    return Path.home() / ".zen"


def arc_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Arc/StorableSidebar.json"
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        candidates = list((root / "Packages").glob("TheBrowserCompany.Arc*/LocalCache/Local/Arc/StorableSidebar.json"))
        if len(candidates) == 1:
            return candidates[0]
    raise ValueError("Cannot find Arc sidebar automatically; provide --arc-json")


def profiles(root: Path) -> list[tuple[str, Path]]:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(root / "profiles.ini")
    result = []
    for section in ini.sections():
        if not section.startswith("Profile"):
            continue
        data = ini[section]
        path = Path(data["Path"])
        if data.get("IsRelative") == "1":
            path = root / path
        if (path / SESSION).exists():
            result.append((data.get("Name", section), path))
    return result


def version_of(profile: Path) -> str:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(profile / "compatibility.ini")
    return ini.get("Compatibility", "LastVersion", fallback="unknown").split("_", 1)[0]


def separate_essentials(profile: Path) -> bool:
    value = True  # Zen 1.23.4b default, covered by the compatibility gate.
    pattern = r'(?m)^\s*user_pref\s*\(\s*"zen\.workspaces\.separate-essentials"\s*,\s*(true|false)\s*\)'
    for filename in ("prefs.js", "user.js"):
        path = profile / filename
        if path.exists():
            for match in re.finditer(pattern, path.read_text()):
                value = match.group(1) == "true"
    return value


def write_new(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def select_space(spaces, selector):
    matches = [s for s in spaces if s["id"] == selector]
    if not matches:
        matches = [s for s in spaces if s.get("title") == selector]
    if len(matches) != 1:
        raise ValueError(f"Ambiguous/missing Arc space {selector!r}; use --list and select by ID")
    return matches[0]


def mapping_options(values, spaces, *, contexts=False):
    result = {}
    for value in values:
        source, separator, target = value.partition("=")
        if not separator or not target:
            raise ValueError("Mappings must be SOURCE=TARGET")
        if contexts:
            matches = {
                arc_data.profile_key(s["profile"])
                for s in spaces
                if arc_data.profile_key(s["profile"]) == source or arc_data.profile_name(s["profile"]) == source
            }
            if len(matches) != 1:
                raise ValueError("Choose a unique Arc profile key from --list for --container")
            result[matches.pop()] = int(target)
        else:
            result[select_space(spaces, source)["id"]] = target
    return result


def wizard(spaces, session, state, args):
    print("Arc → Zen. Cookies, passwords, history and extensions are not migrated.")
    indices = interaction.choose_many(
        "Spaces",
        [f"{space.get('title', 'Untitled')} [{arc_data.profile_name(space['profile'])}]" for space in spaces],
        list(range(len(spaces))),
    )
    selected = [spaces[i] for i in indices]
    mappings = list(args.map)
    explicit = mapping_options(args.map, spaces)
    for source in selected:
        print(f"Destination for {source.get('title') or 'Untitled space'}:")
        candidates = session["spaces"]
        for index, target in enumerate(candidates, 1):
            print(f"  {index}. {target.get('name', 'Untitled')} (container {target.get('containerTabId', 0)})")
        print("  0. Create a new workspace (enter its name)")
        default = (
            explicit.get(source["id"])
            or (args.zen_space if len(selected) == 1 else None)
            or state.get("spaces", {}).get(source["id"])
            or source.get("title")
            or "Untitled space"
        )
        while True:
            value = input(f"Destination: number, name or UUID [{default}]: ").strip() or default
            if value == "0":
                value = (
                    input(f"New workspace name [{source.get('title') or 'Untitled space'}]: ").strip()
                    or source.get("title")
                    or "Untitled space"
                )
                break
            if value.isdigit():
                index = int(value) - 1
                if not 0 <= index < len(candidates):
                    print("Choose a destination from the list, or enter a name.")
                    continue
                value = candidates[index]["uuid"]
            break
        matches = [target for target in candidates if value == target["uuid"]]
        if not matches:
            matches = [target for target in candidates if value == target.get("name")]
        if len(matches) == 1:
            value = matches[0]["uuid"]
        mappings.append(source["id"] + "=" + value)
    args.map = mappings
    args.zen_space = None
    if not args.appearance_only:
        args.duplicates = interaction.choose_one(
            "Manually copied tabs",
            [
                ("skip", "Adopt exact URL + folder path matches"),
                ("keep", "Keep manually copied tabs separate"),
            ],
            "skip",
        )
    args.unsupported = interaction.choose_one(
        "Unsupported sidebar items",
        [
            ("error", "Stop and report unsupported items"),
            ("skip", "Skip unsupported items and report omissions"),
        ],
        args.unsupported,
    )
    return selected


def require_write_support(version: str, allow_untested: bool) -> None:
    if sys.platform != "darwin":
        raise ValueError(
            "Live profile writes are currently supported only on macOS; --output-dir works offline on other systems"
        )
    if version not in TESTED_VERSIONS and not allow_untested:
        raise ValueError(
            f"Untested Zen {version}; tested: {', '.join(sorted(TESTED_VERSIONS))}. "
            "Review a preview first; --allow-untested-version explicitly overrides this gate"
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Migrate Arc spaces into Zen; preview by default, or use --wizard for an interactive import."
    )
    selection = result.add_mutually_exclusive_group()
    selection.add_argument("--space", action="append", help="Arc space name/ID; repeat for several (default: Work)")
    selection.add_argument("--all", action="store_true", help="All Arc spaces")
    result.add_argument("--zen-space", help="Target name/UUID for one selected space")
    result.add_argument(
        "--map", action="append", default=[], metavar="SOURCE=TARGET", help="Map Arc space to Zen name/UUID"
    )
    result.add_argument(
        "--container",
        action="append",
        default=[],
        metavar="PROFILE=ID",
        help="Map Arc profile key/name to existing Zen container",
    )
    result.add_argument("--arc-json", type=Path)
    result.add_argument("--zen-root", type=Path, default=zen_root())
    result.add_argument("--zen-profile", type=Path)
    result.add_argument("--list", action="store_true", help="Show profiles/spaces without importing")
    result.add_argument("--wizard", action="store_true", help="Select, review and optionally apply in one session")
    result.add_argument("--preview-only", action="store_true", help="Never apply; the wizard ends after review")
    result.add_argument("--mode", choices=("add", "update", "skip-existing"), default="add")
    result.add_argument(
        "--duplicates", choices=("keep", "skip"), default="keep", help="Exact URL AND folder path matching"
    )
    result.add_argument("--unsupported", choices=("error", "skip"), default="error")
    result.add_argument("--favorites", choices=("essentials", "folder", "skip"), default="essentials")
    result.add_argument("--no-pinned", action="store_true", help="Omit pinned tabs and folders")
    result.add_argument("--no-unpinned", action="store_true")
    result.add_argument("--no-appearance", action="store_true", help="Keep Zen colors/icons")
    result.add_argument("--appearance-only", action="store_true", help="Only copy colors/icons, without importing tabs")
    result.add_argument("--no-create", action="store_true", help="Require existing target spaces/containers")
    result.add_argument(
        "--allow-untested-version", action="store_true", help="Explicitly allow writing an untested Zen version"
    )
    result.add_argument("--output", type=Path, help="New preview session file (existing containers only)")
    result.add_argument("--output-dir", type=Path, help="New directory with all preview files and report")
    result.add_argument("--report", type=Path, help="Write review report to a NEW JSON file")
    result.add_argument(
        "--preview-html", type=Path, help="NEW offline HTML preview with sidebar tree and change badges"
    )
    result.add_argument(
        "--boosts", action="store_true", help="Also migrate native-compatible Arc Boosts (profile-wide)"
    )
    result.add_argument("--boosts-only", action="store_true", help="Plan Boosts without migrating spaces/tabs")
    result.add_argument(
        "--firestore-cache",
        type=Path,
        default=Path.home() / "Library/Application Support/firestore/Arc/bcny-arc-server/main",
    )
    result.add_argument(
        "--arc-auth", type=Path, default=Path.home() / "Library/Application Support/Arc/StorableAuthState.json"
    )
    result.add_argument("--boost-owner", help="Explicit Arc creator ID if automatic account matching is unavailable")
    boost_selection = result.add_mutually_exclusive_group()
    boost_selection.add_argument(
        "--boost-host", action="append", default=[], help="Only this exact Arc host pattern; repeat to select several"
    )
    boost_selection.add_argument(
        "--boost-id", action="append", default=[], help="Only this Arc Boost ID; repeat to select several"
    )
    result.add_argument(
        "--boost-palette-space", help="Explicit static palette snapshot from this Arc space (not a live Zen binding)"
    )
    result.add_argument("--boosts-json", type=Path, help="Read a prior arc-to-zen Boost export instead of Firestore")
    result.add_argument(
        "--export-boosts", type=Path, help="Preserve original CSS/JS/settings in a NEW private JSON file"
    )
    result.add_argument(
        "--allow-partial-boosts",
        action="store_true",
        help="Import supported parts despite omitted JS/color settings; JS is never executed or migrated natively",
    )
    result.add_argument("--apply", action="store_true", help="Apply with verified backup; requires closed Zen on macOS")
    result.add_argument(
        "--restore", type=Path, metavar="BACKUP", help="Preview selective rollback; add --apply to undo"
    )
    return result


@dataclass
class ImportPlan:
    version: str
    originals: dict[str, bytes | None]
    changes: dict[str, bytes | None]
    session: dict[str, Any]
    result: dict[str, Any]
    state: dict[str, Any]
    trees: list[dict[str, Any]]
    report: dict[str, Any]


def read_profile(profile: Path):
    originals = {
        name: (profile / name).read_bytes() if (profile / name).exists() else None
        for name in (SESSION, "containers.json", STATE_FILE)
    }
    session_raw, containers_raw = originals[SESSION], originals["containers.json"]
    if session_raw is None or containers_raw is None:
        raise ValueError("Initialize Zen once before importing (session/containers missing)")
    session = decode_session(session_raw)
    containers = json.loads(containers_raw)
    state = json.loads(originals[STATE_FILE] or b"{}")
    return originals, session, containers, state


def boost_source(args, data):
    if not args.boosts:
        return None, None
    from arc_to_zen.boosts import load_arc_boosts, load_export, palette_snapshot

    export = (
        load_export(args.boosts_json)
        if args.boosts_json
        else load_arc_boosts(args.firestore_cache, args.arc_auth, owner=args.boost_owner)
    )
    if args.boost_host or args.boost_id:
        key, requested = ("hostPattern", set(args.boost_host)) if args.boost_host else ("id", set(args.boost_id))
        available = {item.get(key) for item in export["boosts"]}
        if requested - available:
            raise ValueError("Some selected Boost hosts/IDs were not found in the source")
        export = {**export, "boosts": [b for b in export["boosts"] if b.get(key) in requested], "omitted": []}
    if args.wizard and export["boosts"]:
        items = export["boosts"]
        indices = interaction.choose_many(
            "Boosts",
            [
                f"{item.get('title', 'Arc Boost')} — {item.get('hostPattern', '')} ({'active' if item.get('active') else 'inactive'})"
                for item in items
            ],
            list(range(len(items))),
        )
        export = {
            **export,
            "boosts": [items[i] for i in indices],
            "omitted": export.get("omitted", []) if len(indices) == len(items) else [],
        }
        palette_mode = interaction.choose_one(
            "Arc CSS variables",
            [
                ("unchanged", "Copy CSS as-is (including --arc-* variables)"),
                ("snapshot", "Add a static palette from one Arc space"),
            ],
            "snapshot" if args.boost_palette_space else "unchanged",
        )
        if palette_mode == "snapshot":
            sidebar = data if data is not None else json.loads((args.arc_json or arc_path()).expanduser().read_text())
            _, palette_spaces = arc_data.catalog(sidebar)
            default = (
                select_space(palette_spaces, args.boost_palette_space)["id"]
                if args.boost_palette_space
                else palette_spaces[0]["id"]
            )
            args.boost_palette_space = interaction.choose_one(
                "Palette space", [(space["id"], space.get("title") or space["id"]) for space in palette_spaces], default
            )
        else:
            args.boost_palette_space = None
    palette = None
    if args.boost_palette_space:
        sidebar = data if data is not None else json.loads((args.arc_json or arc_path()).expanduser().read_text())
        palette = palette_snapshot(sidebar, args.boost_palette_space)
    return export, palette


def prepare_import(args, profile, data, spaces, selected, export, palette) -> ImportPlan:
    version = version_of(profile)
    originals, session, containers, state = read_profile(profile)
    targets = mapping_options(args.map, spaces)
    if args.zen_space:
        if len(selected) != 1:
            raise ValueError("--zen-space requires exactly one selected space; otherwise use --map")
        targets[selected[0]["id"]] = args.zen_space
    context_map = mapping_options(args.container, spaces, contexts=True)
    trees = [
        arc_data.read_space(
            data,
            s["id"],
            unsupported=args.unsupported,
            metadata_only=args.appearance_only,
            sections=[
                name
                for name, enabled in (
                    ("pinned", not args.no_pinned),
                    ("unpinned", not args.no_unpinned),
                    ("essentials", args.favorites != "skip"),
                )
                if enabled
            ],
        )
        for s in selected
    ]
    for tree in trees:
        if args.no_pinned:
            tree["pinned"] = []
        if args.favorites == "skip":
            tree["essentials"] = []
        elif args.favorites == "folder" and tree["essentials"]:
            tree["pinned"].insert(
                0, {"id": "favorites-" + tree["profile_key"], "title": "Favorites", "children": tree["essentials"]}
            )
            tree["essentials"] = []
        if args.no_unpinned:
            tree["unpinned"] = []
    if not args.appearance_only and any(t["essentials"] for t in trees) and not separate_essentials(profile):
        raise ValueError("Enable container-specific Essentials in Zen, or use --favorites folder/skip")
    result, new_contexts, new_state, report = build_plan(
        session,
        containers,
        state,
        trees,
        targets=targets,
        container_map=context_map,
        mode=args.mode,
        copy_appearance=not args.no_appearance,
        duplicates=args.duplicates,
        create=not args.no_create,
        appearance_only=args.appearance_only,
    )
    if args.boosts_only:
        new_state = state  # No sidebar provenance is needed for a Boost-only operation.
    boost_changes = {}
    if args.boosts:
        from arc_to_zen.boosts import BOOST_FILE, plan_boosts

        originals[BOOST_FILE] = (profile / BOOST_FILE).read_bytes() if (profile / BOOST_FILE).exists() else None
        boosts_raw = originals[BOOST_FILE]
        old_boosts = decode_session(boosts_raw) if boosts_raw else {}

        def read_css(name):
            if name not in originals:
                originals[name] = (profile / name).read_bytes() if (profile / name).exists() else None
            return originals[name]

        new_boosts, css_changes, boost_report = plan_boosts(
            old_boosts, export, read_css, mode=args.mode, allow_partial=args.allow_partial_boosts, palette=palette
        )
        boost_changes[BOOST_FILE] = encode_session(new_boosts) if new_boosts != old_boosts else originals[BOOST_FILE]
        for name, raw in css_changes.items():
            read_css(name)
            boost_changes[name] = raw
        # A missing, unchanged store is not a deletion request.
        if boost_changes[BOOST_FILE] is None:
            boost_changes.pop(BOOST_FILE)
        report["boosts"] = boost_report
        report["warnings"].extend(boost_report["warnings"])
    report["zen_version"] = version
    if version not in TESTED_VERSIONS:
        report["warnings"].append(f"Zen {version} has not been tested")
    changes = {
        SESSION: encode_session(result) if result != session else originals[SESSION],
        "containers.json": json_bytes(new_contexts) if new_contexts != containers else originals["containers.json"],
        STATE_FILE: json_bytes(new_state) if new_state != state else originals[STATE_FILE],
    }
    changes = {name: raw for name, raw in changes.items() if raw is not None}
    changes.update(boost_changes)
    if decode_session(changes[SESSION]) != result:
        raise ValueError("Session encoding verification failed")
    return ImportPlan(version, originals, changes, session, result, new_state, trees, report)


def show_plan(profile: Path, args, plan: ImportPlan) -> None:
    version, trees, report = plan.version, plan.trees, plan.report
    print(f"Zen profile: {profile} ({version})")
    for item in report["spaces"]:
        print(f"  {item['space']} → {item['target']}; container {item.get('container', 'unchanged')}")
    for tree in trees:
        if not args.no_appearance:
            metadata = tree["appearance"]
            colors = [c["c"] for c in metadata.get("theme", {}).get("gradientColors", [])]
            print(f"  {tree['name']} style: icon={metadata.get('icon', 'keep')}, colors={', '.join(colors) or 'keep'}")
    added = report["added"]
    print(
        f"Add: {added['tabs']} tabs, {added['folders']} folders, {added['essentials']} Essentials, {added['spaces']} spaces."
    )
    print("Update existing: " + ", ".join(f"{count} {name}" for name, count in report["updated"].items()) + ".")
    appearance = "not selected" if args.boosts_only else "keep Zen" if args.no_appearance else "copy Arc"
    print(f"Mode: {args.mode}; appearance: {appearance}.")
    if args.boosts:
        b = report["boosts"]
        print(
            f"Boosts: {b['added']} new, {b['updated']} updates, {b['existing']} already present, {b['skipped']} skipped; {b['activated']} will activate."
        )
        for item in b["items"]:
            print(
                f"  {item['status'].upper()}: {item['host']} — {item['title']} "
                f"(CSS {item.get('css_bytes', 0)} bytes, zaps {item.get('zaps', 0)})"
            )
            for issue in item["issues"]:
                print("    Note: " + issue)
            for note in item.get("notes", []):
                print("    Note: " + note)
        for item in b.get("omitted", []):
            print(f"  SKIPPED: {item['id']} — {item['reason']}")
    for warning in report["warnings"]:
        print("Warning: " + warning)


def finish_import(args, profile, data, spaces, selected, export, palette, plan: ImportPlan) -> None:
    def save_report():
        if args.report:
            write_new(args.report, json_bytes(plan.report))

    if args.report and args.report.exists():
        raise ValueError("Report output already exists; choose a new path")
    if args.export_boosts and export is not None:
        write_new(args.export_boosts, json_bytes(export))
        print(f"Original Boost sources exported: {args.export_boosts}")
    if args.preview_html:
        from arc_to_zen.preview import html_preview

        write_new(
            args.preview_html, html_preview(plan.session, plan.result, plan.state, plan.trees, plan.report).encode()
        )
        print(f"HTML preview: {args.preview_html}")
    if args.output_dir:
        args.output_dir.mkdir(mode=0o700)
        for name, raw in plan.changes.items():
            if raw is not None:
                (args.output_dir / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                write_new(args.output_dir / name, raw)
        write_new(args.output_dir / "report.json", json_bytes(plan.report))
        print(f"Preview files: {args.output_dir}")
    elif args.output:
        if any(name not in (SESSION, STATE_FILE) and raw != plan.originals[name] for name, raw in plan.changes.items()):
            raise ValueError("New containers or Boosts require --output-dir with all companion files")
        session_output = plan.changes[SESSION]
        if session_output is None:
            raise ValueError("Preview session is missing")
        write_new(args.output, session_output)
        print(f"Preview session: {args.output}")
    elif args.apply or (args.wizard and not args.preview_only and not args.preview_html):
        if not any(raw != plan.originals[name] for name, raw in plan.changes.items()):
            save_report()
            print("Nothing to change.")
            return
        require_write_support(plan.version, args.allow_untested_version)
        while True:
            if args.wizard:
                if input("Apply this plan with a backup? [y/N]: ").strip().lower() not in ("y", "yes"):
                    save_report()
                    print("Preview complete. No Zen files changed.")
                    return
                if not interaction.prepare_browser(profile):
                    save_report()
                    print("Cancelled. No Zen files changed.")
                    return
                refreshed = prepare_import(args, profile, data, spaces, selected, export, palette)
                require_write_support(refreshed.version, args.allow_untested_version)
                if refreshed.originals != plan.originals or refreshed.version != plan.version:
                    plan = refreshed
                    print("Zen saved changes after the preview. Updated plan using your existing choices:")
                    show_plan(profile, args, plan)
                    continue
                plan = refreshed
            try:
                backup = commit(profile, plan.originals, plan.changes, plan.report)
            except (ProfileBusyError, ProfileChangedError) as error:
                if not args.wizard:
                    raise
                print(
                    f"{error}\nYour choices are kept. The profile will be checked and the plan refreshed before retrying."
                )
                continue
            print(f"Applied. Verified backup: {backup}" if backup else "Nothing to change.")
            save_report()
            return
    else:
        print("Preview only. Quit Zen, then add --apply to import.")
    save_report()


def main(argv: list[str] | None = None) -> None:
    options = parser()
    args = options.parse_args(argv)
    args.boosts = (
        args.boosts
        or args.boosts_only
        or bool(
            args.wizard
            and (
                args.boosts_json
                or args.export_boosts
                or args.boost_host
                or args.boost_id
                or args.boost_palette_space
                or args.allow_partial_boosts
            )
        )
    )
    if args.boosts_only and (args.space or args.all or args.appearance_only):
        options.error("--boosts-only cannot be combined with space selection or --appearance-only")
    if (
        args.boosts_json
        or args.export_boosts
        or args.allow_partial_boosts
        or args.boost_palette_space
        or args.boost_host
        or args.boost_id
    ) and not args.boosts:
        options.error("Boost-specific options require --boosts or --boosts-only")
    if args.output and args.output_dir:
        options.error("Choose --output or --output-dir")
    if args.preview_only and args.apply:
        options.error("--preview-only cannot be combined with --apply")
    if (args.output or args.output_dir or args.preview_html) and args.apply:
        options.error("Preview outputs cannot be combined with --apply")
    if args.no_appearance and args.appearance_only:
        options.error("--appearance-only cannot be combined with --no-appearance")
    if args.restore and (
        args.output or args.output_dir or args.preview_html or args.wizard or args.space or args.all or args.boosts
    ):
        options.error("--restore cannot be combined with import/preview selection")
    choices = profiles(args.zen_root)
    if args.zen_profile:
        choices = [("Selected", args.zen_profile.expanduser())]
    if args.list:
        data = json.loads((args.arc_json or arc_path()).expanduser().read_text())
        _, spaces = arc_data.catalog(data)
        for name, path in choices:
            print(f"Zen profile: {name}: {path}")
            for space in decode_session((path / SESSION).read_bytes())["spaces"]:
                print(f"  {space.get('name')!r}: {space['uuid']}; container {space.get('containerTabId', 0)}")
            if (path / "containers.json").exists():
                for context in json.loads((path / "containers.json").read_text()).get("identities", []):
                    if context.get("public"):
                        print(f"  Container {context['userContextId']}: {context.get('name', '(built-in)')}")
        for space in spaces:
            print(
                f"Arc {space.get('title')!r}: {space['id']}; profile "
                f"{arc_data.profile_name(space['profile'])!r} ({arc_data.profile_key(space['profile'])})"
            )
        return
    if args.wizard and not sys.stdin.isatty():
        raise ValueError("--wizard needs an interactive terminal")
    if args.wizard and args.zen_profile is None and len(choices) > 1:
        choice = interaction.choose_one(
            "Zen profile", [(str(index), f"{name}: {path}") for index, (name, path) in enumerate(choices, 1)], "1"
        )
        args.zen_profile = choices[int(choice) - 1][1]
    profile = (args.zen_profile or find_profile(args.zen_root)).expanduser().resolve()
    version = version_of(profile)
    if args.apply:
        require_write_support(version, args.allow_untested_version)
    if args.restore:
        manifest = json.loads((args.restore / "manifest.json").read_text())
        if manifest.get("version") != 2:
            if args.restore.resolve().parent != (profile / "arc-to-zen-backups").resolve():
                raise ValueError("Choose a backup belonging to the selected Zen profile")
            if args.apply:
                restore(profile, args.restore)
                print("Original legacy session restored.")
            else:
                print("Legacy backup: whole-session rollback only. Add --apply to restore before restarting Zen.")
            return
        originals, changes, report = rollback_plan(profile, args.restore)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.apply:
            backup = commit(profile, originals, changes, report)
            print(f"Rollback applied. Safety backup: {backup}" if backup else "Nothing to undo.")
        else:
            print("Rollback preview only; add --apply to undo this import.")
        return
    if args.wizard:
        interaction.features(args)
    if args.boosts_only:
        data, spaces, selected = None, [], []
    else:
        data = json.loads((args.arc_json or arc_path()).expanduser().read_text())
        _, spaces = arc_data.catalog(data)
        if args.wizard:
            _, session, _, state = read_profile(profile)
            selected = wizard(spaces, session, state, args)
        elif args.all:
            selected = spaces
        else:
            selected = [select_space(spaces, name) for name in (args.space or ["Work"])]
    if len({space["id"] for space in selected}) != len(selected):
        raise ValueError("The same Arc space was selected more than once")
    export, palette = boost_source(args, data)
    plan = prepare_import(args, profile, data, spaces, selected, export, palette)
    show_plan(profile, args, plan)
    finish_import(args, profile, data, spaces, selected, export, palette, plan)
