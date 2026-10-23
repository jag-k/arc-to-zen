"""Read Arc sidebar structure and translate supported appearance metadata."""

import hashlib
import json
import math
from typing import Any


ICON_BASE = "chrome://browser/skin/zen-icons/selectable/"
# Inventory from Zen 1.23.4b; avoid emitting nonexistent chrome:// resources.
ICONS = {
    "airplane",
    "american-football",
    "baseball",
    "basket",
    "bed",
    "bell",
    "book",
    "bookmark",
    "briefcase",
    "brush",
    "bug",
    "build",
    "cafe",
    "call",
    "card",
    "chat",
    "checkbox",
    "circle",
    "cloud",
    "code",
    "coins",
    "construct",
    "cutlery",
    "egg",
    "extension-puzzle",
    "eye",
    "fast-food",
    "fish",
    "flag",
    "flame",
    "flask",
    "folder",
    "game-controller",
    "globe-1",
    "globe",
    "grid-2x2",
    "grid-3x3",
    "heart",
    "ice-cream",
    "image",
    "inbox",
    "key",
    "layers",
    "leaf",
    "lightning",
    "location",
    "lock-closed",
    "logo-github",
    "logo-rss",
    "logo-usd",
    "mail",
    "map",
    "megaphone",
    "moon",
    "music",
    "navigate",
    "nuclear",
    "page",
    "palette",
    "paw",
    "people",
    "pizza",
    "planet",
    "present",
    "rocket",
    "school",
    "shapes",
    "shirt",
    "skull",
    "square",
    "squares",
    "star-1",
    "star",
    "stats-chart",
    "sun",
    "tada",
    "terminal",
    "ticket",
    "time",
    "trash",
    "triangle",
    "video",
    "volume-high",
    "wallet",
    "warning",
    "water",
    "weight",
}


def paired(values):
    if not isinstance(values, list) or len(values) % 2:
        raise ValueError("Invalid Arc key/value pairs")
    return zip(values[::2], values[1::2], strict=False)


def catalog(data):
    containers = data.get("sidebar", {}).get("containers", [])
    matches = [value for key, value in paired(containers) if isinstance(key, dict) and "global" in key]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise ValueError("Expected one Arc global sidebar container")
    container = matches[0]
    spaces = [s for s in container.get("spaces", []) if isinstance(s, dict)]
    if any(not isinstance(s.get("id"), str) or not s["id"] for s in spaces) or len(
        {s.get("id") for s in spaces}
    ) != len(spaces):
        raise ValueError("Duplicate or missing Arc space IDs")
    return container, spaces


def profile_key(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def profile_name(profile):
    if "default" in profile:
        return "Default"
    return profile.get("custom", {}).get("_0", {}).get("directoryBasename", "Arc profile")


def icon(info, warnings, *, folder=False):
    value = info.get("iconType", {}) if isinstance(info, dict) else {}
    if "icon" in value:
        name = value["icon"]
        name = {"github": "logo-github", "construction": "construct"}.get(name, name)
        if name in ICONS:
            return ICON_BASE + name + ".svg"
        warnings.append(f"Unknown Arc icon {name!r}; existing/default icon retained")
    if "emoji" in value:
        emoji = value["emoji"]
        if isinstance(emoji, dict):
            emoji = emoji.get("_0")
        if isinstance(emoji, str) and len(emoji) <= 32:
            if not folder:
                return emoji
            warnings.append("Folder emoji cannot be mapped to a Zen folder image")
    return None


def appearance(custom, warnings):
    """Copy RGB hues; Arc's materials, layout and light/dark palettes differ."""
    result = {}
    converted_icon = icon(custom, warnings)
    if converted_icon:
        result["icon"] = converted_icon
    theme = custom.get("windowTheme", {})
    background = theme.get("background", {})
    colors, modifiers = [], []

    def inspect(value):
        if isinstance(value, dict):
            if all(k in value for k in ("red", "green", "blue")):
                channels = [value[k] for k in ("red", "green", "blue")]
                if not all(isinstance(c, (int, float)) and math.isfinite(c) for c in channels):
                    raise ValueError("Invalid Arc theme RGB channels")
                if value.get("colorSpace", "sRGB") not in ("sRGB", "extendedSRGB"):
                    warnings.append("Non-sRGB Arc color approximated as sRGB")
                if any(c < 0 or c > 1 for c in channels):
                    warnings.append("Extended RGB components clipped to the sRGB gamut")
                rgba = [round(min(1, max(0, c)) * 255) for c in channels]
                alpha = value.get("alpha", 1)
                if not isinstance(alpha, (int, float)) or not math.isfinite(alpha):
                    raise ValueError("Invalid Arc color alpha")
                color = "#" + "".join(f"{c:02x}" for c in rgba)
                if alpha < 1:
                    color += f"{round(min(1, max(0, alpha)) * 255):02x}"
                colors.append(color)
                return
            if isinstance(value.get("modifiers"), dict):
                modifiers.append(value["modifiers"])
            for key, child in value.items():
                if key != "modifiers":
                    inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(background)
    if not colors:
        palette = theme.get("primaryColorPalette", {})
        fallback = palette.get("shaded") or palette.get("midTone")
        if fallback:
            inspect(fallback)
            warnings.append("Arc background variant unsupported; palette color used")
    if colors:
        if len(colors) > 4:
            warnings.append("Arc gradient has over four colors; first four retained")
        texture = 0
        if modifiers:
            noise = modifiers[0].get("noiseFactor", 0)
            if isinstance(noise, (int, float)) and math.isfinite(noise):
                texture = round(min(1, max(0, noise)) * 16) / 16
        result["theme"] = {
            "type": "gradient",
            "gradientColors": [
                {
                    "c": color,
                    "isCustom": True,
                    "algorithm": "floating",
                    "isPrimary": i == 0,
                    "lightness": 50,
                    "position": {"x": 160 + i * 20, "y": 160},
                    "type": "explicit-lightness",
                }
                for i, color in enumerate(colors[:4])
            ],
            "opacity": 1,
            "texture": texture,
        }
        warnings.append(
            "RGB colors copied; Arc vibrancy, gradient geometry, materials and light/dark adaptation are approximate"
        )
    return result


def read_space(data, selector, *, unsupported="error", metadata_only=False, sections=None):
    container, spaces = catalog(data)
    matches = [s for s in spaces if s.get("id") == selector]
    if not matches:
        matches = [s for s in spaces if s.get("title") == selector]
    if len(matches) != 1:
        raise ValueError(f"Select a unique Arc space by name or ID: {selector!r}")
    space = matches[0]
    values = [x for x in container.get("items", []) if isinstance(x, dict)]
    items = {x["id"]: x for x in values}
    if len(items) != len(values):
        raise ValueError("Duplicate Arc item IDs")
    warnings, skipped = [], []

    def reject(item_id, reason):
        message = f"Arc item {item_id}: {reason}"
        if unsupported == "error":
            raise ValueError(message + "; use --unsupported skip to omit it explicitly")
        skipped.append(message)

    def children(item_id, ancestors=frozenset()):
        if item_id in ancestors:
            raise ValueError(f"Arc cycle at {item_id}")
        if item_id not in items:
            raise ValueError(f"Missing Arc item {item_id}")
        if len(ancestors) >= 64:
            raise ValueError("Arc folder depth exceeds 64")
        result = []
        for child_id in items[item_id]["childrenIds"]:
            if child_id == item_id or child_id in ancestors:
                raise ValueError(f"Arc cycle at {child_id}")
            if child_id not in items:
                raise ValueError(f"Missing Arc child {child_id}")
            child = items[child_id]
            child_data = child.get("data", {})
            tab = child_data.get("tab")
            if isinstance(tab, dict):
                if child.get("childrenIds"):
                    reject(child_id, "tab with children/split view is unsupported")
                    continue
                url = tab.get("savedURL")
                if not isinstance(url, str) or not url:
                    reject(child_id, "no saved URL")
                    continue
                result.append({"id": child_id, "url": url, "title": child.get("title") or tab.get("savedTitle") or url})
            elif "list" in child_data:
                node = {
                    "id": child_id,
                    "title": child.get("title") or "Untitled folder",
                    "children": children(child_id, ancestors | {item_id}),
                }
                info = child_data["list"]
                if "automaticLiveFolderData" in info:
                    warnings.append(f"Dynamic folder {child_id} becomes an ordinary folder")
                image = icon(info.get("customInfo", {}), warnings, folder=True)
                if image:
                    node["icon"] = image
                result.append(node)
            else:
                reject(child_id, "unsupported data type " + ", ".join(child_data))
        return result

    tree: dict[str, Any] = {
        "pinned": [],
        "unpinned": [],
        "essentials": [],
        "warnings": warnings,
        "skipped_items": skipped,
        "space_id": space["id"],
        "name": space.get("title") or "Untitled space",
        "profile_key": profile_key(space["profile"]),
        "profile_name": profile_name(space["profile"]),
    }
    if metadata_only:
        tree["appearance"] = appearance(space.get("customInfo", {}), warnings)
        return tree
    included = {"pinned", "unpinned", "essentials"} if sections is None else set(sections)
    for kind, item_id in paired(space["newContainerIDs"]):
        section = next((s for s in ("pinned", "unpinned") if s in kind), None)
        if section is None:
            raise ValueError("Unknown Arc container type")
        if section in included:
            tree[section].extend(children(item_id))
    for profile, item_id in paired(container.get("topAppsContainerIDs", [])):
        if "essentials" in included and profile == space["profile"]:
            tree["essentials"].extend(children(item_id))
    tree["appearance"] = appearance(space.get("customInfo", {}), warnings)
    return tree
