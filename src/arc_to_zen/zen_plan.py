"""Build a validated migration in memory, preserving unrelated browser state."""

import copy
import time
import uuid

from arc_to_zen.session import migrate


STATE_FILE = "arc-to-zen-state.json"
TESTED_VERSIONS = {"1.23.4b"}


def identity(kind, value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"arc-to-zen:{kind}:{value}"))


def validate(session, containers):
    for section in ("tabs", "folders", "groups", "spaces"):
        if not isinstance(session.get(section), list):
            raise ValueError(f"Unsupported Zen session: {section} must be a list")
    for section, key in (("tabs", "zenSyncId"), ("folders", "id"), ("groups", "id"), ("spaces", "uuid")):
        ids = [item.get(key) for item in session[section]]
        if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError(f"Missing/duplicate Zen {section} identifiers")
    identities = containers.get("identities")
    if not isinstance(identities, list) or not isinstance(containers.get("lastUserContextId"), int):
        raise ValueError("Invalid Zen containers.json")
    public = {0} | {x.get("userContextId") for x in identities if x.get("public") is True}
    for space in session["spaces"]:
        context = space.get("containerTabId", 0)
        if type(context) is not int or context < 0 or context >= 4294967295 or context not in public:
            raise ValueError(f"Space {space.get('name')!r} references nonexistent container {context}")
    folder_ids = {f["id"] for f in session["folders"]}
    group_ids = {g["id"] for g in session["groups"]}
    tabs = {t["zenSyncId"]: t for t in session["tabs"]}
    for folder in session["folders"]:
        if folder["id"] not in group_ids:
            raise ValueError("Zen folder has no corresponding group")
        parent = folder.get("parentId")
        if parent and parent not in folder_ids:
            raise ValueError("Zen folder has a missing parent")
        if any(tab_id not in tabs for tab_id in folder.get("emptyTabIds", [])):
            raise ValueError("Zen folder has a missing placeholder")
    parents = {f["id"]: f.get("parentId") for f in session["folders"]}
    for folder_id in parents:
        seen, current = set(), folder_id
        while current:
            if current in seen:
                raise ValueError("Cycle in Zen folders")
            seen.add(current)
            current = parents.get(current)


def tab_url(tab):
    initial = tab.get("_zenPinnedInitialState", {}).get("entry", {})
    if initial.get("url"):
        return initial["url"]
    entries = tab.get("entries", [])
    index = max(0, min(len(entries) - 1, (tab.get("index") or 1) - 1))
    return entries[index].get("url") if entries else None


def folder_paths(folders):
    index = {f["id"]: f for f in folders}
    result = {}

    def path(folder_id, seen=frozenset()):
        if not folder_id:
            return ()
        if folder_id in seen:
            raise ValueError("Cycle in folder paths")
        folder = index.get(folder_id)
        if not folder:
            return ("<unknown>", folder_id)
        return (*path(folder.get("parentId"), seen | {folder_id}), folder.get("name", ""))

    for folder_id in index:
        result[folder_id] = path(folder_id)
    return result


def merge_order(existing, desired, key, *, update=False):
    """Insert additions beside their source neighbors; update mode reorders managed nodes."""
    desired_ids = {x[key] for x in desired}
    known = {x[key]: x for x in existing}
    if update:
        # Retain a tab's runtime state; update only imported structure and saved URL/label.
        ordered = []
        for item in desired:
            old = known.get(item[key])
            if old and key == "zenSyncId":
                value = copy.deepcopy(old)
                for field in (
                    "groupId",
                    "zenWorkspace",
                    "zenStaticLabel",
                    "pinned",
                    "zenEssential",
                    "userContextId",
                    "_zenPinnedInitialState",
                    "zenIsEmpty",
                ):
                    if field in item:
                        value[field] = copy.deepcopy(item[field])
                    else:
                        value.pop(field, None)
                if "_zenPinnedInitialState" in value and "_zenPinnedInitialState" in old:
                    value["_zenPinnedInitialState"]["image"] = old["_zenPinnedInitialState"].get("image")
                if tab_url(old) != tab_url(item):
                    value["entries"] = copy.deepcopy(item["entries"])
                    value["index"] = 1
                ordered.append(value)
            elif old:
                # Keep native fields absent from our model, e.g. future Zen metadata.
                value = {**copy.deepcopy(old), **copy.deepcopy(item)}
                if "collapsed" in old:
                    value["collapsed"] = old["collapsed"]
                ordered.append(value)
            else:
                ordered.append(copy.deepcopy(item))
        first = next((i for i, x in enumerate(existing) if x[key] in desired_ids), len(existing))
        untouched = [x for x in existing if x[key] not in desired_ids]
        insertion = sum(x[key] not in desired_ids for x in existing[:first])
        return untouched[:insertion] + ordered + untouched[insertion:]
    result = list(existing)
    for i, item in enumerate(desired):
        if item[key] in known:
            continue
        following = next((x[key] for x in desired[i + 1 :] if x[key] in known), None)
        previous = next((x[key] for x in reversed(desired[:i]) if x[key] in known), None)
        if following:
            position = next(j for j, x in enumerate(result) if x[key] == following)
        elif previous:
            position = next(j for j, x in enumerate(result) if x[key] == previous) + 1
        elif key == "zenSyncId" and item.get("zenEssential"):
            position = next((j for j, x in enumerate(result) if not x.get("zenEssential")), len(result))
        elif key == "zenSyncId" and item.get("pinned"):
            position = next((j for j, x in enumerate(result) if not x.get("pinned")), len(result))
        else:
            position = len(result)
        result.insert(position, copy.deepcopy(item))
        known[item[key]] = item
    return result


def remap(desired, aliases):
    for tab in desired["tabs"]:
        tab["zenSyncId"] = aliases.get(tab["zenSyncId"], tab["zenSyncId"])
        if tab.get("groupId"):
            tab["groupId"] = aliases.get(tab["groupId"], tab["groupId"])
    for section in ("folders", "groups"):
        for node in desired[section]:
            node["id"] = aliases.get(node["id"], node["id"])
            if section == "folders":
                if node.get("parentId"):
                    node["parentId"] = aliases.get(node["parentId"], node["parentId"])
                prev = node.get("prevSiblingInfo", {})
                if prev:
                    prev["id"] = aliases.get(prev.get("id"), prev.get("id"))
                node["emptyTabIds"] = [aliases.get(i, i) for i in node["emptyTabIds"]]


def duplicate_aliases(existing, desired, aliases, *, adopt=False):
    """Match by exact URL + space/container + folder path, never by URL alone."""
    old_paths, new_paths = folder_paths(existing["folders"]), folder_paths(desired["folders"])
    old_folders = {}
    for folder in existing["folders"]:
        key = (folder.get("workspaceId"), old_paths[folder["id"]])
        old_folders.setdefault(key, []).append(folder)
    known = {t["zenSyncId"] for t in existing["tabs"]}
    count = 0

    def signature(tab, paths):
        return (
            tab.get("zenWorkspace"),
            tab.get("userContextId", 0),
            bool(tab.get("zenEssential")),
            bool(tab.get("pinned")),
            paths.get(tab.get("groupId"), ()),
            tab_url(tab),
        )

    old_tabs = {}
    for tab in existing["tabs"]:
        if not tab.get("zenIsEmpty"):
            old_tabs.setdefault(signature(tab, old_paths), []).append(tab)
    claimed = set(aliases.values())
    for tab in desired["tabs"]:
        if tab["zenSyncId"] in known or tab.get("zenIsEmpty"):
            continue
        candidates = [t for t in old_tabs.get(signature(tab, new_paths), []) if t["zenSyncId"] not in claimed]
        if len(candidates) == 1:
            count += 1
            if adopt:
                aliases[tab["zenSyncId"]] = candidates[0]["zenSyncId"]
                claimed.add(candidates[0]["zenSyncId"])
    if adopt:
        for folder in desired["folders"]:
            candidates = old_folders.get((folder.get("workspaceId"), new_paths[folder["id"]]), [])
            if len(candidates) == 1 and folder["id"] != candidates[0]["id"]:
                aliases[folder["id"]] = candidates[0]["id"]
                old = candidates[0]
                anchor = next(
                    (t for t in existing["tabs"] if t.get("zenIsEmpty") and t.get("groupId") == old["id"]), None
                )
                own_anchor = next(
                    (t for t in desired["tabs"] if t.get("zenIsEmpty") and t.get("groupId") == folder["id"]), None
                )
                if anchor and own_anchor:
                    aliases[own_anchor["zenSyncId"]] = anchor["zenSyncId"]
    return count


def build_plan(
    session,
    containers,
    state,
    trees,
    *,
    targets=None,
    container_map=None,
    mode="add",
    copy_appearance=True,
    duplicates="keep",
    create=True,
    appearance_only=False,
):
    validate(session, containers)
    if not isinstance(state, dict) or any(
        not isinstance(state.get(k, {}), dict) for k in ("spaces", "profiles", "aliases")
    ):
        raise ValueError("Invalid migration state file")
    result, contexts, journal = copy.deepcopy(session), copy.deepcopy(containers), copy.deepcopy(state)
    journal.setdefault("version", 1)
    if journal["version"] != 1:
        raise ValueError("Unsupported migration state version")
    journal.setdefault("spaces", {})
    journal.setdefault("profiles", {})
    journal.setdefault("aliases", {})
    aliases = journal["aliases"]
    existing_ids = {t["zenSyncId"] for t in result["tabs"]} | {f["id"] for f in result["folders"]}
    aliases = {k: v for k, v in aliases.items() if v in existing_ids}
    targets, container_map = targets or {}, container_map or {}
    warnings, summaries = [], []
    selected_targets, source_contexts, desired_contexts = set(), {}, {}
    all_desired = {"tabs": [], "folders": [], "groups": []}
    # Infer sharing before creating anything: a new space may precede an existing peer.
    for tree in trees:
        mapped = journal["spaces"].get(tree["space_id"])
        if mapped and not any(s["uuid"] == mapped for s in result["spaces"]):
            mapped = None
        target = targets.get(tree["space_id"]) or mapped or tree["name"]
        candidates = [s for s in result["spaces"] if s["uuid"] == target]
        if not candidates:
            candidates = [s for s in result["spaces"] if s.get("name") == target]
        if len(candidates) == 1:
            key, context = tree["profile_key"], candidates[0].get("containerTabId", 0)
            if key in journal["profiles"] and journal["profiles"][key] != context:
                raise ValueError("An existing mapped space changed containers; review mappings before importing")
            if key in source_contexts and source_contexts[key] != context:
                raise ValueError("Spaces sharing an Arc profile must map to the same Zen container")
            source_contexts[key] = context
    for tree in trees:
        warnings.extend(tree["warnings"])
        warnings.extend(tree["skipped_items"])
        source_id, source_profile = tree["space_id"], tree["profile_key"]
        mapped = journal["spaces"].get(source_id)
        if mapped and not any(s["uuid"] == mapped for s in result["spaces"]):
            mapped = None
        target = targets.get(source_id) or mapped or tree["name"]
        matches = [s for s in result["spaces"] if s["uuid"] == target]
        if not matches:
            matches = [s for s in result["spaces"] if s.get("name") == target]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous Zen space {target!r}; map by UUID")
        space = matches[0] if matches else None
        explicit = container_map.get(source_profile)
        mapped_context = journal["profiles"].get(source_profile)
        if source_profile in source_contexts:
            mapped_context = source_contexts[source_profile]
        if space is None:
            if not create:
                raise ValueError(f"Zen space {target!r} does not exist; allow creation")
            context = explicit if explicit is not None else mapped_context
            if context is None:
                context = (
                    max(
                        [contexts["lastUserContextId"]]
                        + [x.get("userContextId", 0) for x in contexts["identities"] if x.get("public")]
                    )
                    + 1
                )
                contexts["lastUserContextId"] = context
                contexts["identities"].append(
                    {
                        "userContextId": context,
                        "public": True,
                        "name": "Arc: " + tree["profile_name"],
                        "icon": "fingerprint",
                        "color": "purple",
                    }
                )
            space = {
                "uuid": "{" + identity("space", source_id) + "}",
                "name": target,
                "icon": "",
                "theme": {"type": "gradient", "gradientColors": [], "opacity": 1, "texture": 0},
                "containerTabId": context,
                "hasCollapsedPinnedTabs": False,
            }
            result["spaces"].append(space)
        elif explicit is not None and explicit != space.get("containerTabId", 0):
            raise ValueError("Refusing to change the container of an existing Zen space; use a new target space")
        context = space.get("containerTabId", 0)
        valid_contexts = {0} | {x.get("userContextId") for x in contexts["identities"] if x.get("public")}
        if context not in valid_contexts:
            raise ValueError(f"Container {context} does not exist")
        if mapped_context is not None and mapped_context != context:
            raise ValueError("Spaces sharing an Arc profile must map to the same Zen container")
        if context in desired_contexts and desired_contexts[context] != source_profile:
            raise ValueError("Different Arc profiles cannot share a Zen container; create separate target spaces")
        if any(key != source_profile and value == context for key, value in journal["profiles"].items()):
            raise ValueError("This Zen container is already associated with another Arc profile")
        if space["uuid"] in selected_targets:
            raise ValueError("Two Arc spaces cannot be merged into the same Zen space in one run")
        selected_targets.add(space["uuid"])
        desired_contexts[context] = source_profile
        source_contexts[source_profile] = context
        journal["spaces"][source_id] = space["uuid"]
        journal["profiles"][source_profile] = context
        if (
            not appearance_only
            and mode == "skip-existing"
            and any(t.get("zenWorkspace") == space["uuid"] and not t.get("zenIsEmpty") for t in session["tabs"])
        ):
            summaries.append(
                {"space": tree["name"], "target": space["name"], "container": context, "skipped_space": True}
            )
            continue
        if copy_appearance:
            space.update(copy.deepcopy(tree["appearance"]))
        if appearance_only:
            summaries.append(
                {
                    "space": tree["name"],
                    "target": space["name"],
                    "container": context,
                    "appearance_only": True,
                    "appearance": sorted(tree["appearance"]) if copy_appearance else [],
                }
            )
            continue
        blank = {**result, "tabs": [], "folders": [], "groups": []}
        generated, counts = migrate(blank, tree, space["name"], reference=result)
        desired = {k: generated[k] for k in all_desired}
        for folder in desired["folders"]:
            if not folder.get("parentId") and folder.get("prevSiblingInfo", {}).get("type") == "start":
                folder["prevSiblingInfo"] = None
        remap(desired, aliases)
        additions = duplicate_aliases(result, desired, aliases, adopt=duplicates == "skip")
        if additions:
            warnings.append(
                f"{tree['name']}: {additions} exact URL/path matches "
                + ("adopted" if duplicates == "skip" else "will remain separate (--duplicates skip to adopt)")
            )
        remap(desired, aliases)
        for section in all_desired:
            known = {x.get("zenSyncId") or x.get("id") for x in all_desired[section]}
            all_desired[section].extend(x for x in desired[section] if (x.get("zenSyncId") or x.get("id")) not in known)
        summaries.append(
            {
                "space": tree["name"],
                "target": space["name"],
                "container": context,
                "source_tabs": counts["tabs"],
                "source_folders": counts["folders"],
                "source_essentials": counts["essentials"],
                "omitted": len(tree["skipped_items"]),
                "appearance": sorted(tree["appearance"]) if copy_appearance else [],
            }
        )
    journal["aliases"] = aliases
    # Order NEW templates by section without rearranging existing cross-space state.
    all_desired["tabs"].sort(key=lambda t: 0 if t.get("zenEssential") else 1 if t.get("pinned") else 2)
    if mode == "add":
        differences = 0
        for section, key, fields in (
            ("tabs", "zenSyncId", ("zenStaticLabel", "groupId", "zenWorkspace")),
            ("folders", "id", ("name", "parentId")),
        ):
            old = {x[key]: x for x in session[section]}
            for item in all_desired[section]:
                current = old.get(item[key])
                if current and (
                    any(current.get(k) != item.get(k) for k in fields)
                    or (section == "tabs" and tab_url(current) != tab_url(item))
                ):
                    differences += 1
            common = {x[key] for x in all_desired[section]} & set(old)
            if [x[key] for x in session[section] if x[key] in common] != [
                x[key] for x in all_desired[section] if x[key] in common
            ]:
                warnings.append(
                    f"{section}: existing order differs from Arc; add mode preserves it (--mode update to reorder)"
                )
        if differences:
            warnings.append(
                f"{differences} existing items differ from Arc; add mode preserves edits (--mode update to update them)"
            )
    for section, key in (("tabs", "zenSyncId"), ("folders", "id"), ("groups", "id")):
        result[section] = merge_order(result[section], all_desired[section], key, update=mode == "update")
    # Existing folders need the newly created nested anchors too. Do not remove native anchors.
    planned_folders = {f["id"]: f for f in all_desired["folders"]}
    for folder in result["folders"]:
        if folder["id"] in planned_folders:
            folder["emptyTabIds"] = list(
                dict.fromkeys(folder.get("emptyTabIds", []) + planned_folders[folder["id"]]["emptyTabIds"])
            )
    if result != session:
        result["lastCollected"] = int(time.time() * 1000)
    validate(result, contexts)
    added = {
        section: len(result[section]) - len(session[section]) for section in ("tabs", "folders", "groups", "spaces")
    }
    old_tabs = {t["zenSyncId"] for t in session["tabs"]}
    added["essentials"] = sum(t.get("zenEssential", False) for t in result["tabs"] if t["zenSyncId"] not in old_tabs)
    added["placeholders"] = sum(t.get("zenIsEmpty", False) for t in result["tabs"] if t["zenSyncId"] not in old_tabs)
    added["tabs"] -= added["essentials"] + added["placeholders"]
    changed = {}
    for section, key in (("tabs", "zenSyncId"), ("folders", "id"), ("groups", "id"), ("spaces", "uuid")):
        old = {x[key]: x for x in session[section]}
        changed[section] = sum(x[key] in old and x != old[x[key]] for x in result[section])
    report = {
        "spaces": summaries,
        "added": added,
        "updated": changed,
        "warnings": list(dict.fromkeys(warnings)),
        "mode": mode,
    }
    return result, contexts, journal, report
