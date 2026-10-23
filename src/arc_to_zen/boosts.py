"""Read Arc Boosts locally and plan conversion into native Zen Boosts."""

import copy
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any

from arc_to_zen.firestore_proto import document
from arc_to_zen.leveldb_readonly import snapshot


BOOST_FILE = "zen-boosts.jsonlz4"
EXPORT_FORMAT = "arc-to-zen-boosts-v1"


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def load_arc_boosts(cache, auth_file, *, owner=None):
    rows = snapshot(cache, key_filter=lambda key: b"remote_document\x00" in key and b"boosts\x00" in key)
    documents = []
    for key, raw in rows.items():
        if b"remote_document\x00" not in key or b"boosts\x00" not in key or not raw:
            continue
        parsed = document(raw)
        if parsed and parsed[0].split("/documents/", 1)[1].startswith("boosts/"):
            documents.append(parsed)
    creators = {v["creatorID"] for _, v in documents if v.get("creatorID")}
    if not documents:
        return {"format": EXPORT_FORMAT, "boosts": [], "omitted": []}
    if owner is None:
        auth = json.loads(Path(auth_file).read_text())
        matches = creators & set(strings(auth))
        if len(matches) != 1:
            raise ValueError("Cannot identify the current Arc Boost owner; provide --boost-owner explicitly")
        owner = matches.pop()
    if documents and owner not in creators:
        raise ValueError("No cached Boosts match the selected owner")
    boosts, omitted = [], []
    for name, fields in documents:
        if fields.get("creatorID") != owner:
            continue
        source_id = name.rsplit("/", 1)[1]
        if not fields.get("data"):
            omitted.append({"id": source_id, "reason": "Incomplete cached record: no Boost data"})
            continue
        data = json.loads(fields["data"])
        if not isinstance(data, dict):
            raise ValueError("Unsupported Arc Boost payload")
        active = fields.get("isActive", False)
        if active not in (True, False, "true", "false"):
            raise ValueError("Unsupported Arc Boost activation value")
        boosts.append(
            {
                "id": source_id,
                "title": fields.get("title") or fields.get("hostPattern") or "Arc Boost",
                "hostPattern": fields.get("hostPattern", ""),
                "active": active in (True, "true"),
                "data": data,
            }
        )
    return {"format": EXPORT_FORMAT, "boosts": boosts, "omitted": omitted}


def load_export(path):
    data = json.loads(Path(path).read_text())
    if data.get("format") != EXPORT_FORMAT or not isinstance(data.get("boosts"), list):
        raise ValueError("Expected an arc-to-zen-boosts-v1 export")
    ids = []
    for boost in data["boosts"]:
        if (
            not isinstance(boost.get("id"), str)
            or not boost["id"]
            or not isinstance(boost.get("data"), dict)
            or type(boost.get("active")) is not bool
        ):
            raise ValueError("Invalid exported Boost")
        ids.append(boost["id"])
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate exported Boost IDs")
    return data


def native_defaults():
    # Zen 1.23.4b nsZenBoostsManager.getEmptyBoostEntry().
    return {
        "boostName": "Arc Boost",
        "dotAngleDeg": 131.61,
        "dotPos": {"x": 0.76, "y": 0.66},
        "dotDistance": 0.91,
        "secondaryDotAngleDegDelta": 55,
        "secondaryDotPos": {"x": 0.5, "y": 0.81},
        "brightness": 0.5,
        "saturation": 0.5,
        "contrast": 0.75,
        "fontFamily": "",
        "enableColorBoost": False,
        "smartInvert": False,
        "autoTheme": False,
        "textCaseOverride": "none",
        "sizeOverride": 1,
        "zapSelectors": [],
        "focusSelectors": [],
        "changeWasMade": True,
    }


def domain_for(pattern):
    if not isinstance(pattern, str) or not pattern or pattern != pattern.strip():
        return None
    # Native Zen indexes exact hostnames and discards ports. Never widen a scope.
    try:
        domain = pattern.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if len(domain) > 253 or any(c in domain for c in "*:/?#[]\\"):
        return None
    labels = domain.split(".")
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        return None
    return domain


def convert(source):
    payload = source["data"]
    native = native_defaults()
    native["boostName"] = source.get("title") or "Arc Boost"
    css = payload.get("css", "")
    zaps = payload.get("zaps", [])
    js = payload.get("js", "")
    if (
        not isinstance(css, str)
        or not isinstance(js, str)
        or not isinstance(zaps, list)
        or not all(isinstance(z, str) for z in zaps)
    ):
        raise ValueError("Unsupported Arc CSS/JavaScript/zap data types")
    native["zapSelectors"] = zaps
    unsupported = []
    if js.strip():
        unsupported.append("JavaScript has no native Zen Boost equivalent; preserved only in the export")
    if payload.get("colorScheme"):
        unsupported.append("Arc colorScheme is not equivalent to Zen color transformations")
    font = payload.get("font")
    if font:
        if isinstance(font, dict) and isinstance(font.get("family"), str) and not (set(font) - {"family"}):
            family = font["family"].replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            generic = {
                "serif",
                "sans-serif",
                "monospace",
                "cursive",
                "fantasy",
                "system-ui",
                "ui-serif",
                "ui-sans-serif",
                "ui-monospace",
                "ui-rounded",
                "emoji",
                "math",
                "fangsong",
            }
            native["fontFamily"] = family if family.lower() in generic else '"' + family + '"'
        else:
            unsupported.append("Unsupported Arc font variant")
    transform = payload.get("textTransform", "none")
    if transform in ("none", "uppercase", "lowercase", "capitalize"):
        native["textCaseOverride"] = transform
    else:
        unsupported.append("Unsupported text transform")
    zoom = payload.get("textZoom")
    if zoom:
        level = zoom.get("level") if isinstance(zoom, dict) else None
        if (
            isinstance(level, (int, float))
            and not isinstance(level, bool)
            and math.isfinite(level)
            and 10 <= level <= 500
        ):
            native["sizeOverride"] = level / 100
        else:
            unsupported.append("Unsupported text zoom")
    for key in set(payload) - {"css", "zaps", "js", "colorScheme", "font", "textTransform", "textZoom"}:
        unsupported.append("Unsupported Arc property: " + key)
    usable = bool(
        css.strip()
        or zaps
        or native["fontFamily"]
        or native["textCaseOverride"] != "none"
        or native["sizeOverride"] != 1
    )
    return {"boostData": native}, css, unsupported, usable


def palette_snapshot(sidebar, selector):
    """Explicit static Arc palette, not a promise to follow Zen workspace changes."""
    from arc_to_zen.arc_data import catalog

    _, spaces = catalog(sidebar)
    matches = [s for s in spaces if s["id"] == selector]
    if not matches:
        matches = [s for s in spaces if s.get("title") == selector]
    if len(matches) != 1:
        raise ValueError("Choose a unique Arc space for --boost-palette-space")
    theme = matches[0].get("customInfo", {}).get("windowTheme", {})
    palettes = theme.get("semanticColorPalette", {}).get("appearanceBased", {})
    if not palettes:
        raise ValueError("The selected Arc space has no supported semantic palette")

    def rgba(color):
        channels = [color.get(k) for k in ("red", "green", "blue", "alpha")]
        if not all(type(c) in (int, float) and math.isfinite(c) and 0 <= c <= 1 for c in channels):
            raise ValueError("Unsupported Arc palette color")
        return "rgba(" + ", ".join(str(round(c * 255)) for c in channels[:3]) + ", " + str(channels[3]) + ")"

    def background_colors(value):
        if isinstance(value, dict):
            if all(k in value for k in ("red", "green", "blue", "alpha")):
                yield rgba(value)
            else:
                for child in value.values():
                    yield from background_colors(child)
        elif isinstance(value, list):
            for child in value:
                yield from background_colors(child)

    backgrounds = list(background_colors(theme.get("background", {})))
    blocks, names = [], set()
    for mode in ("light", "dark"):
        palette = palettes.get(mode)
        if not isinstance(palette, dict):
            raise ValueError("Both light and dark Arc palettes are required")
        values = {"--arc-palette-" + k: rgba(v) for k, v in palette.items() if re.fullmatch("[A-Za-z][A-Za-z0-9]*", k)}
        base = backgrounds[0] if backgrounds else values.get("--arc-palette-background")
        if base:
            values["--arc-background-simple-color"] = base
            # Arc's overlay/material shader cannot be reproduced: use explicit static colors.
            for i in range(2):
                value = backgrounds[min(i, len(backgrounds) - 1)] if backgrounds else base
                values["--arc-background-gradient-color" + str(i)] = value
                values["--arc-background-gradient-overlay-color" + str(i)] = value
        names.update(values)
        declarations = "".join(f"{key}: {value};\n" for key, value in values.items())
        blocks.append(f"@media (prefers-color-scheme: {mode}) {{\n:root {{\n{declarations}}}\n}}\n")
    return {
        "css": "/* Static Arc palette snapshot; not a live Zen workspace binding. */\n" + "".join(blocks),
        "names": names,
        "space": matches[0].get("title") or matches[0]["id"],
    }


def plan_boosts(existing, export, read_css, *, mode="add", allow_partial=False, palette=None):
    if not isinstance(existing, dict):
        raise ValueError("Invalid Zen Boost store")
    result, files = copy.deepcopy(existing), {}
    report: dict[str, Any] = {
        "added": 0,
        "updated": 0,
        "existing": 0,
        "activated": 0,
        "skipped": len(export.get("omitted", [])),
        "items": [],
        "warnings": ["Native Zen Boosts apply to the entire browser profile, not individual spaces/containers."],
    }
    candidates = {}
    if palette:
        report["warnings"].append(
            f"Boost colors use a static snapshot of Arc space {palette['space']!r}; they do not follow Zen workspace changes. Gradient overlays are approximate."
        )
    for source in export["boosts"]:
        domain = domain_for(source.get("hostPattern"))
        item = {
            "id": source["id"],
            "title": source.get("title", "Arc Boost"),
            "host": source.get("hostPattern", ""),
            "source_active": bool(source.get("active")),
            "issues": [],
        }
        report["items"].append(item)
        if not domain:
            item.update(status="skipped", issues=["Host pattern cannot be represented as an exact Zen hostname"])
            report["skipped"] += 1
            continue
        entry, css, unsupported, usable = convert(source)
        clean_css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        used = set(re.findall(r"var\(\s*(--arc-[\w-]+)", clean_css))
        declared = set(re.findall(r"(--arc-[\w-]+)\s*:", clean_css))
        missing = used - declared - (palette["names"] if palette else set())
        if missing:
            item["notes"] = [
                "CSS is copied unchanged, including unresolved --arc-* variables; --boost-palette-space is optional"
            ]
        if palette and used:
            css = palette["css"] + "\n" + css
        item.update(css_bytes=len(css.encode()), zaps=len(entry["boostData"]["zapSelectors"]), issues=unsupported)
        if not usable or (unsupported and not allow_partial):
            item["status"] = "skipped"
            if not usable:
                item["issues"].append("No transferable CSS/zaps/font/text settings")
            report["skipped"] += 1
            continue
        boost_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "arc-to-zen:boost:" + source["id"]))
        host = result.setdefault(domain, {"activeBoostId": None, "boostEntries": {}})
        if not isinstance(host.get("boostEntries"), dict):
            raise ValueError("Invalid Zen Boost entries")
        old = host["boostEntries"].get(boost_id)
        if old is not None and mode != "update":
            item["status"] = "already present"
            report["existing"] += 1
            continue
        path = "zen-boosts/" + boost_id + ".css"
        css_bytes = css.encode()
        if old == entry and read_css(path) == css_bytes:
            item["status"] = "already present"
            report["existing"] += 1
        else:
            item["status"] = "will update" if old is not None else "will add"
            report["updated" if old is not None else "added"] += 1
            host["boostEntries"][boost_id] = entry
            # Keep even an empty CSS file in the transaction; native Zen may remove it later.
            files[path] = css_bytes if css_bytes else None
        if source.get("active"):
            candidates.setdefault(domain, []).append(boost_id)
    for domain, ids in candidates.items():
        host = result[domain]
        active = host.get("activeBoostId")
        if not active and len(ids) == 1:
            host["activeBoostId"] = ids[0]
            report["activated"] += 1
        elif not active and len(ids) > 1:
            report["warnings"].append(
                f"{domain}: several Arc Boosts are active; Zen supports one. Imported without activating one arbitrarily."
            )
        elif active not in ids:
            report["warnings"].append(f"{domain}: existing active Zen Boost retained")
    report["omitted"] = export.get("omitted", [])
    if any(item.get("issues") for item in report["items"]):
        report["warnings"].append(
            "Some Boosts are incomplete or unsupported; use --export-boosts to preserve source CSS, JavaScript and settings."
        )
    return result, files, report


def rollback_boosts(profile, before_files, after_files, current_files):
    from arc_to_zen.session import decode_session, encode_session

    before = decode_session(before_files[BOOST_FILE]) if before_files[BOOST_FILE] else {}
    after = decode_session(after_files[BOOST_FILE]) if after_files[BOOST_FILE] else {}
    current = decode_session(current_files[BOOST_FILE]) if current_files[BOOST_FILE] else {}
    result, changes, conflicts, edited = copy.deepcopy(current), {}, [], set()
    for name in after_files:
        if not name.startswith("zen-boosts/"):
            continue
        actual, expected, previous = current_files[name], after_files[name], before_files[name]
        # Native Zen removes empty CSS sidecars during its own save.
        if (actual or b"") == (expected or b""):
            changes[name] = previous
        elif (actual or b"") != (previous or b""):
            edited.add(Path(name).stem)
            conflicts.append("Retained edited Boost CSS: " + name)
    for domain, snapshot_entry in after.items():
        if domain not in result:
            continue
        live = result[domain]
        old_entries = before.get(domain, {}).get("boostEntries", {})
        for boost_id, new_entry in snapshot_entry.get("boostEntries", {}).items():
            if new_entry == old_entries.get(boost_id):
                continue
            actual = live.get("boostEntries", {}).get(boost_id)
            if actual is None:
                continue
            if boost_id in edited or actual != new_entry:
                edited.add(boost_id)
                conflicts.append("Retained edited Boost: " + boost_id)
                continue
            if boost_id in old_entries:
                live["boostEntries"][boost_id] = copy.deepcopy(old_entries[boost_id])
            else:
                live["boostEntries"].pop(boost_id, None)
        active = live.get("activeBoostId")
        if active == snapshot_entry.get("activeBoostId") and active not in edited:
            live["activeBoostId"] = before.get(domain, {}).get("activeBoostId")
        if not live.get("boostEntries") and domain not in before:
            result.pop(domain)
    for name in list(changes):
        if Path(name).stem in edited:
            changes.pop(name)
    changes[BOOST_FILE] = encode_session(result)
    return changes, conflicts
