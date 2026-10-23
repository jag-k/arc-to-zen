#!/usr/bin/env python3
"""Import an Arc space into an existing Zen space (preview unless --apply)."""

import configparser
import copy
import hashlib
import json
import os
import struct
import subprocess
import tempfile
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MAGIC = b"mozLz40\0"
SESSION = "zen-sessions.jsonlz4"
MAX_SIZE = 256 * 1024 * 1024


class ProfileChangedError(ValueError):
    """The profile changed before a transaction started; a new plan is required."""


class ProfileBusyError(ValueError):
    """The browser must release its profile before the prepared plan can be applied."""


def decode_session(raw: bytes) -> dict[str, Any]:
    """Decode Mozilla's size-prefixed raw LZ4 block without dependencies."""
    if raw[:8] != MAGIC or len(raw) < 13:
        raise ValueError("Not a Mozilla jsonlz4 session")
    size = struct.unpack_from("<I", raw, 8)[0]
    if size > MAX_SIZE:
        raise ValueError("Session exceeds the 256 MiB limit")
    source, out, pos = raw[12:], bytearray(), 0

    def length(initial):
        nonlocal pos
        result = initial
        if initial == 15:
            while True:
                extra = source[pos]
                pos += 1
                result += extra
                if extra != 255:
                    break
        return result

    try:
        while pos < len(source):
            token = source[pos]
            pos += 1
            literals = length(token >> 4)
            if pos + literals > len(source) or len(out) + literals > size:
                raise ValueError("Invalid LZ4 literal length")
            out.extend(source[pos : pos + literals])
            pos += literals
            if pos == len(source):
                break
            offset = source[pos] | source[pos + 1] << 8
            pos += 2
            matches = length(token & 15) + 4
            if not offset or offset > len(out) or len(out) + matches > size:
                raise ValueError("Invalid LZ4 match")
            for _ in range(matches):
                out.append(out[-offset])
    except IndexError as error:
        raise ValueError("Truncated LZ4 session") from error
    if len(out) != size:
        raise ValueError("LZ4 size mismatch")
    return json.loads(out)


def encode_session(data: dict[str, Any]) -> bytes:
    # A literal-only LZ4 block is valid for Mozilla and avoids native dependencies.
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    if len(payload) > MAX_SIZE:
        raise ValueError("Session exceeds the 256 MiB limit")
    block = bytearray([min(len(payload), 15) << 4])
    if len(payload) >= 15:
        remaining = len(payload) - 15
        while remaining >= 255:
            block.append(255)
            remaining -= 255
        block.append(remaining)
    block.extend(payload)
    return MAGIC + struct.pack("<I", len(payload)) + block


def arc_space(data, name):
    from arc_to_zen.arc_data import read_space

    return read_space(data, name)


def migrate(session, tree, space_name, *, reference=None):
    """Add imported nodes; preserve existing session objects and deterministic IDs."""
    for key in ("tabs", "folders", "groups", "spaces"):
        if not isinstance(session.get(key), list):
            raise ValueError(f"Unsupported Zen session: missing {key}")
    matches = [s for s in session["spaces"] if s.get("name") == space_name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one Zen space named {space_name!r}")
    workspace = matches[0]
    workspace_id = workspace["uuid"]
    context = workspace.get("containerTabId", 0)
    if type(context) is not int or context < 0:
        raise ValueError("Target space must have a valid container ID")
    reference = reference or session
    result = copy.deepcopy(session)
    tab_ids = {t["zenSyncId"] for t in result["tabs"] if t.get("zenSyncId")}
    folder_ids = {f["id"] for f in result["folders"]}
    group_ids = {g["id"] for g in result["groups"]}
    stats = {"tabs": 0, "essentials": 0, "folders": 0, "placeholders": 0, "skipped": 0}
    added_pins, added_essentials, added_normal = [], [], []
    now = int(time.time() * 1000)

    def ident(kind, source_id):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"arc-to-zen:{workspace_id}:{kind}:{source_id}"))

    def make_tab(source, *, pinned=True, essential=False, group=None, empty=False):
        if essential:
            key = tree.get("profile_key", "legacy-profile")
            tab_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"arc-to-zen:essential:{context}:{key}:{source['id']}"))
            # Adopt Essentials made by the first release without duplicating them.
            legacy = {
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"arc-to-zen:{s['uuid']}:tab:{source['id']}"))
                for s in reference["spaces"]
                if s.get("containerTabId", 0) == context
            }
            existing = next(
                (
                    t
                    for t in reference["tabs"]
                    if t.get("zenEssential") and t.get("userContextId", 0) == context and t.get("zenSyncId") in legacy
                ),
                None,
            )
            if existing:
                tab_id = existing["zenSyncId"]
        else:
            tab_id = ident("placeholder" if empty else "tab", source["id"])
        if tab_id in tab_ids:
            stats["skipped"] += not empty
            return None
        url = "about:blank" if empty else source["url"]
        entry = {"url": url, "title": "" if empty else source["title"]}
        tab = {
            "entries": [entry],
            "index": 1,
            "lastAccessed": now,
            "pinned": pinned,
            "hidden": False,
            "zenWorkspace": None if essential else workspace_id,
            "zenSyncId": tab_id,
            "zenEssential": essential,
            "zenDefaultUserContextId": "true",
            "zenIsEmpty": empty,
            "zenHasStaticIcon": False,
            "zenPinnedIcon": None,
            "zenGlanceId": None,
            "zenIsGlance": False,
            "zenLiveFolderItemId": None,
            "_zenIsActiveTab": True,
            "userContextId": context,
            "attributes": {},
        }
        if group:
            tab["groupId"] = group
        if pinned:
            tab["_zenPinnedInitialState"] = {"entry": copy.deepcopy(entry), "image": None}
        if not empty:
            tab["zenStaticLabel"] = source["title"]
        tab_ids.add(tab_id)
        stats["placeholders" if empty else "essentials" if essential else "tabs"] += 1
        return tab

    def emit(nodes, output, parent=None, *, pinned=True, essential=False):
        prev = {"type": "start", "id": None}
        placeholders = []
        for source in nodes:
            if "children" not in source:
                tab = make_tab(source, pinned=pinned, essential=essential, group=parent)
                if tab:
                    output.append(tab)
                prev = {"type": "tab", "id": ident("tab", source["id"])}
                continue
            if essential or not pinned:
                raise ValueError("Folders in Arc favorites/unpinned sections are not supported")
            folder_id = ident("folder", source["id"])
            anchor_id = ident("placeholder", source["id"])
            anchor = make_tab(source, group=folder_id, empty=True)
            if anchor:
                output.append(anchor)
            folder = None
            if folder_id not in folder_ids:
                if folder_id in group_ids:
                    raise ValueError("Imported group exists without its folder")
                folder = {
                    "id": folder_id,
                    "name": source["title"],
                    "pinned": True,
                    "splitViewGroup": False,
                    "collapsed": True,
                    "saveOnWindowClose": True,
                    "parentId": parent,
                    "prevSiblingInfo": prev.copy(),
                    "emptyTabIds": [],
                    "userIcon": source.get("icon", ""),
                    "isLiveFolder": False,
                    "workspaceId": workspace_id,
                }
                result["folders"].append(folder)
                result["groups"].append(
                    {
                        "id": folder_id,
                        "name": source["title"],
                        "pinned": True,
                        "splitView": False,
                        "color": "zen-workspace-color",
                        "collapsed": True,
                        "saveOnWindowClose": True,
                    }
                )
                folder_ids.add(folder_id)
                group_ids.add(folder_id)
                stats["folders"] += 1
            nested = emit(source["children"], output, folder_id)
            if folder is not None:
                folder["emptyTabIds"] = [anchor_id, *nested]
            placeholders.extend([anchor_id, *nested])
            prev = {"type": "group", "id": folder_id}
        return placeholders

    emit(tree["essentials"], added_essentials, essential=True)
    emit(tree["pinned"], added_pins)
    emit(tree["unpinned"], added_normal, pinned=False)
    # Keep old objects in the same relative order; pinned tabs precede normal tabs.
    tabs = result["tabs"]
    essential_end = next((i for i, t in enumerate(tabs) if not t.get("zenEssential")), len(tabs))
    tabs[essential_end:essential_end] = added_essentials
    pinned_end = next((i for i, t in enumerate(tabs) if not t.get("pinned")), len(tabs))
    tabs[pinned_end:pinned_end] = added_pins
    tabs.extend(added_normal)
    if any(stats[k] for k in ("tabs", "essentials", "folders", "placeholders")):
        result["lastCollected"] = now
    return result, stats


def find_profile(root: Path) -> Path:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(root / "profiles.ini")
    installs = [s for s in ini.sections() if s.startswith("Install")]
    install_paths = {default for s in installs if (default := ini[s].get("Default"))}
    if len(install_paths) == 1:
        return root / install_paths.pop()
    if len(install_paths) > 1:
        raise ValueError("Several Zen installations have different profiles; use --wizard or --zen-profile")
    profiles = [s for s in ini.sections() if s.startswith("Profile")]
    defaults = [s for s in profiles if ini[s].get("Default") == "1"]
    if len(defaults) != 1:
        raise ValueError("Cannot select Zen profile automatically; use --zen-profile")
    section = ini[defaults[0]]
    return (root if section.get("IsRelative") == "1" else Path("/")) / section["Path"]


def atomic_write(path: Path, raw: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".arc-to-zen-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def closed_profile(profile: Path) -> Generator[None]:
    import fcntl

    # Do not quit the browser automatically: the user may have unsaved work.
    probe = subprocess.run(["pgrep", "-ix", "zen"], capture_output=True)
    if probe.returncode == 0:
        raise ProfileBusyError("Zen is running. Quit Zen with Cmd+Q before applying.")
    if probe.returncode != 1:
        raise ValueError("Could not check Zen processes; refusing to write")
    lock = profile / ".parentlock"
    if not lock.exists():
        raise ValueError("Zen profile has no .parentlock; refusing to write")
    with lock.open("r+b") as stream:
        try:
            fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ProfileBusyError("Zen profile is locked; quit Zen first") from error
        yield


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def apply_session(profile, original, converted, stats):
    path = profile / SESSION
    with closed_profile(profile):
        if path.read_bytes() != original:
            raise ValueError("Zen session changed during preparation; run again")
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        backup = profile / "arc-to-zen-backups" / stamp
        backup.mkdir(parents=True, mode=0o700)
        backup.parent.chmod(0o700)
        atomic_write(backup / SESSION, original)
        manifest = {"before_sha256": digest(original), "after_sha256": digest(converted), "stats": stats}
        atomic_write(backup / "manifest.json", json.dumps(manifest, indent=2).encode())
        if (backup / SESSION).read_bytes() != original:
            raise ValueError("Backup verification failed")
        atomic_write(path, converted)
        if path.read_bytes() != converted:
            raise ValueError(f"Write verification failed; backup: {backup}")
        return backup


def restore(profile, backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    original = (backup / SESSION).read_bytes()
    if digest(original) != manifest["before_sha256"]:
        raise ValueError("Backup checksum mismatch")
    decode_session(original)
    with closed_profile(profile):
        current = (profile / SESSION).read_bytes()
        if digest(current) != manifest["after_sha256"]:
            raise ValueError("Session changed since import; automatic rollback would lose changes")
        atomic_write(profile / SESSION, original)
