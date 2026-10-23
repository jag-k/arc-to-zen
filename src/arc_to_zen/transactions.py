"""Checked multi-file writes and three-way, import-specific rollback."""

import copy
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from arc_to_zen.session import (
    SESSION,
    ProfileChangedError,
    atomic_write,
    closed_profile,
    decode_session,
    digest,
    encode_session,
)
from arc_to_zen.zen_plan import STATE_FILE, validate


ALLOWED_FILES = {SESSION, "containers.json", STATE_FILE, "zen-boosts.jsonlz4"}


def allowed_file(name):
    return name in ALLOWED_FILES or re.fullmatch(r"zen-boosts/[a-f0-9-]{36}\.css", name) is not None


def read_optional(path):
    return path.read_bytes() if path.exists() else None


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def commit(profile, originals, changes, report):
    """Back up all affected files first; undo completed writes if a later write fails."""
    if any(not allowed_file(name) for name in set(originals) | set(changes)) or set(changes) - set(originals):
        raise ValueError("Invalid transaction file set")
    changes = {name: value for name, value in changes.items() if originals[name] != value}
    if not changes:
        return None
    with closed_profile(profile):
        for name in originals:
            if (profile / name).is_symlink():
                raise ValueError("Refusing to replace a symlinked profile file")
            current = (profile / name).read_bytes() if (profile / name).exists() else None
            if current != originals[name]:
                raise ProfileChangedError(f"{name} changed during preparation; refresh the plan")
        root = profile / "arc-to-zen-backups"
        if root.is_symlink():
            raise ValueError("Backup directory must not be a symlink")
        root.mkdir(mode=0o700, exist_ok=True)
        root.chmod(0o700)
        backup = root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%fZ")
        backup.mkdir(mode=0o700)
        files = {}
        for name, after in changes.items():
            before = originals[name]
            (backup / ("before-" + name)).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            (backup / ("after-" + name)).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if before is not None:
                atomic_write(backup / ("before-" + name), before)
                if (backup / ("before-" + name)).read_bytes() != before:
                    raise ValueError("Backup readback verification failed")
            if after is not None:
                atomic_write(backup / ("after-" + name), after)
            files[name] = {
                "before": digest(before) if before is not None else None,
                "after": digest(after) if after is not None else None,
            }
        manifest = {
            "version": 2,
            "profile": str(profile.resolve()),
            "status": "prepared",
            "files": files,
            "report": report,
        }
        atomic_write(backup / "manifest.json", json_bytes(manifest))
        written = []
        try:
            # Container definitions must be present before a session can reference them.
            for name in sorted(changes, key=lambda n: n in (SESSION, "zen-boosts.jsonlz4")):
                parent = (profile / name).parent
                if parent.is_symlink():
                    raise ValueError("Refusing to write through a symlinked profile subdirectory")
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if changes[name] is None:
                    (profile / name).unlink(missing_ok=True)
                else:
                    atomic_write(profile / name, changes[name])
                written.append(name)
                if read_optional(profile / name) != changes[name]:
                    raise ValueError(f"Readback verification failed for {name}")
        except BaseException as error:
            failures = []
            for name in reversed(written):
                try:
                    if originals[name] is None:
                        (profile / name).unlink(missing_ok=True)
                    else:
                        atomic_write(profile / name, originals[name])
                except OSError:
                    failures.append(name)
            manifest["status"] = "rollback-failed" if failures else "rolled-back"
            manifest["rollback_failures"] = failures
            atomic_write(backup / "manifest.json", json_bytes(manifest))
            if failures:
                raise ValueError(f"Partial transaction rollback failed for {failures}; backup: {backup}") from error
            raise
        manifest["status"] = "committed"
        atomic_write(backup / "manifest.json", json_bytes(manifest))
        return backup


TAB_FIELDS = ("zenWorkspace", "userContextId", "zenEssential", "pinned", "groupId", "zenStaticLabel", "zenIsEmpty")
FOLDER_FIELDS = ("name", "workspaceId", "parentId", "prevSiblingInfo", "userIcon", "pinned")
SPACE_FIELDS = ("name", "icon", "theme", "containerTabId")


def semantic(item, section):
    if section == "tabs":
        from arc_to_zen.zen_plan import tab_url

        return {**{k: item.get(k) for k in TAB_FIELDS}, "savedURL": tab_url(item)}
    fields = (
        FOLDER_FIELDS
        if section == "folders"
        else SPACE_FIELDS
        if section == "spaces"
        else ("name", "color", "pinned", "splitView", "saveOnWindowClose")
    )
    result = {k: item.get(k) for k in fields}
    if section == "folders" and not result.get("prevSiblingInfo"):
        # Zen serializes a first root folder as null, but a first nested one as start.
        result["prevSiblingInfo"] = {"type": "start", "id": None}
    return result


def undo_session(before, after, current):
    """Remove unchanged additions and revert unedited fields; keep user changes."""
    result, conflicts = copy.deepcopy(current), []
    keys = {"tabs": "zenSyncId", "folders": "id", "groups": "id", "spaces": "uuid"}
    added, retained = {}, {}
    for section, key in keys.items():
        old = {x[key]: x for x in before[section]}
        new = {x[key]: x for x in after[section]}
        live = {x[key]: x for x in current[section]}
        added[section] = set(new) - set(old)
        retained[section] = {
            i for i in added[section] if i in live and semantic(live[i], section) != semantic(new[i], section)
        }
    # Keep folders containing user-created/moved/edited children, plus their ancestors.
    live_folders = {f["id"]: f for f in current["folders"]}
    retained_groups = set(retained["folders"]) | set(retained["groups"])
    for tab in current["tabs"]:
        if (tab["zenSyncId"] not in added["tabs"] or tab["zenSyncId"] in retained["tabs"]) and tab.get("groupId"):
            retained_groups.add(tab["groupId"])
    for folder in current["folders"]:
        if (folder["id"] not in added["folders"] or folder["id"] in retained["folders"]) and folder.get("parentId"):
            retained_groups.add(folder["parentId"])
    pending = list(retained_groups)
    while pending:
        parent = live_folders.get(pending.pop(), {}).get("parentId")
        if parent and parent not in retained_groups:
            retained_groups.add(parent)
            pending.append(parent)
    retained["folders"] |= retained_groups & added["folders"]
    retained["groups"] |= retained_groups & added["groups"]
    # A retained imported folder needs its empty anchor even if all imported URLs are removed.
    for tab in current["tabs"]:
        if tab.get("zenIsEmpty") and tab.get("groupId") in retained_groups:
            retained["tabs"].add(tab["zenSyncId"])
    kept_tabs = [
        t for t in current["tabs"] if t["zenSyncId"] not in added["tabs"] or t["zenSyncId"] in retained["tabs"]
    ]
    for space in current["spaces"]:
        if any(t.get("zenWorkspace") == space["uuid"] for t in kept_tabs):
            retained["spaces"].add(space["uuid"])
    for section, key in keys.items():
        old = {x[key]: x for x in before[section]}
        new = {x[key]: x for x in after[section]}
        output = []
        for item in result[section]:
            ident = item[key]
            if ident in added[section]:
                if ident not in retained[section]:
                    continue
                conflicts.append(f"Retained edited/in-use {section} item {ident}")
            elif ident in old and ident in new:
                for field in set(old[ident]) | set(new[ident]):
                    old_value, new_value = old[ident].get(field), new[ident].get(field)
                    if old_value == new_value:
                        continue
                    if item.get(field) == new_value:
                        if field in old[ident]:
                            item[field] = copy.deepcopy(old_value)
                        else:
                            item.pop(field, None)
                    else:
                        conflicts.append(f"Kept edited {section}.{field} on {ident}")
            output.append(item)
        result[section] = output
    # Zen may have appended newly nested placeholder IDs during its own save.
    remaining_tabs = {t["zenSyncId"] for t in result["tabs"]}
    for folder in result["folders"]:
        folder["emptyTabIds"] = [i for i in folder.get("emptyTabIds", []) if i in remaining_tabs]
    # Restore prior managed order only when the relevant sequence itself was not edited.
    for section, key in keys.items():
        original_ids = {x[key] for x in before[section]}
        expected = [x[key] for x in after[section] if x[key] in original_ids]
        observed = [x[key] for x in current[section] if x[key] in original_ids]
        if expected == observed:
            positions = [i for i, x in enumerate(result[section]) if x[key] in original_ids]
            lookup = {x[key]: x for x in result[section]}
            ordered = [lookup[x[key]] for x in before[section] if x[key] in lookup]
            for position, item in zip(positions, ordered, strict=False):
                result[section][position] = item
        elif expected != [x[key] for x in before[section]]:
            conflicts.append(f"Kept edited {section} order")
    return result, conflicts


def rollback_plan(profile, backup):
    backup = Path(backup).resolve()
    if backup.parent != (profile / "arc-to-zen-backups").resolve():
        raise ValueError("Choose a backup belonging to the selected Zen profile")
    manifest = json.loads((backup / "manifest.json").read_text())
    if manifest.get("version") != 2 or manifest.get("status") != "committed":
        raise ValueError("Selective rollback requires a committed version-2 import")
    if manifest.get("profile") != str(profile.resolve()):
        raise ValueError("Backup belongs to another Zen profile")
    if any(not allowed_file(name) for name in manifest["files"]):
        raise ValueError("Invalid backup file names")
    originals, stored_before, stored_after = {}, {}, {}
    for name, checksums in manifest["files"].items():
        before = (backup / ("before-" + name)).read_bytes() if checksums["before"] else None
        after = (backup / ("after-" + name)).read_bytes() if checksums["after"] else None
        if (digest(before) if before is not None else None) != checksums["before"] or (
            digest(after) if after is not None else None
        ) != checksums["after"]:
            raise ValueError("Backup checksum mismatch")
        stored_before[name], stored_after[name] = before, after
        originals[name] = (profile / name).read_bytes() if (profile / name).exists() else None
    # Include the unchanged session/containers too: deletion decisions depend on references.
    for name in (SESSION, "containers.json"):
        originals.setdefault(name, (profile / name).read_bytes())
    current = decode_session(originals[SESSION])
    conflicts, result = [], current
    if SESSION in stored_after:
        result, conflicts = undo_session(
            decode_session(stored_before[SESSION]), decode_session(stored_after[SESSION]), current
        )
    containers = json.loads(originals["containers.json"])
    if "containers.json" in stored_after:
        old = json.loads(stored_before["containers.json"])
        new = json.loads(stored_after["containers.json"])
        added = {x["userContextId"] for x in new["identities"]} - {x["userContextId"] for x in old["identities"]}
        used = {s.get("containerTabId", 0) for s in result["spaces"]} | {
            t.get("userContextId", 0) for t in result["tabs"]
        }
        new_index = {x["userContextId"]: x for x in new["identities"]}
        containers["identities"] = [
            x
            for x in containers["identities"]
            if x["userContextId"] not in added or x["userContextId"] in used or x != new_index[x["userContextId"]]
        ]
        # Never decrement lastUserContextId: Firefox should not reuse an old identity.
    changes = {
        SESSION: originals[SESSION] if result == current else encode_session(result),
        "containers.json": originals["containers.json"]
        if containers == json.loads(originals["containers.json"])
        else json_bytes(containers),
    }
    if "zen-boosts.jsonlz4" in stored_after:
        from arc_to_zen.boosts import rollback_boosts

        boost_changes, boost_conflicts = rollback_boosts(profile, stored_before, stored_after, originals)
        changes.update(boost_changes)
        conflicts.extend(boost_conflicts)
    if STATE_FILE in stored_after:
        # Preserve provenance after selective rollback so a later import can reuse mappings.
        # Drop aliases to deleted objects; mappings to removed spaces/contexts must be rebuilt.
        state = json.loads(originals[STATE_FILE] or b"{}")
        valid_spaces = {s["uuid"] for s in result["spaces"]}
        valid_contexts = {0} | {x["userContextId"] for x in containers["identities"] if x.get("public")}
        valid_ids = {t["zenSyncId"] for t in result["tabs"]} | {f["id"] for f in result["folders"]}
        for key, valid in (("spaces", valid_spaces), ("profiles", valid_contexts), ("aliases", valid_ids)):
            state[key] = {k: v for k, v in state.get(key, {}).items() if v in valid}
        changes[STATE_FILE] = json_bytes(state)
    validate(result, containers)
    return originals, changes, {"rollback_of": backup.name, "warnings": conflicts}
