"""Self-contained, offline HTML review of the actual planned Zen session."""

import re
from html import escape
from urllib.parse import urlsplit

from arc_to_zen.zen_plan import tab_url


def html_preview(before, after, state, trees, report):
    old_tabs = {t["zenSyncId"]: t for t in before["tabs"]}
    old_folders = {f["id"]: f for f in before["folders"]}
    old_spaces = {s["uuid"]: s for s in before["spaces"]}
    positions = {t["zenSyncId"]: i for i, t in enumerate(after["tabs"])}

    def badge(old, new):
        label, kind = (
            ("Will be added", "add")
            if old is None
            else ("Already present", "keep")
            if old == new
            else ("Will change", "edit")
        )
        return f'<span class="badge {kind}">{label}</span>'

    def color(theme):
        dots = (theme or {}).get("gradientColors", [])
        values = []
        for dot in dots:
            value = dot.get("c")
            if (
                isinstance(value, list)
                and len(value) >= 3
                and all(type(c) in (int, float) and 0 <= c <= 255 for c in value[:3])
            ):
                value = "#" + "".join(f"{round(c):02x}" for c in value[:3])
            if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value):
                values.append(value)
        return (
            "".join(f'<span class="color" style="background:{v}"></span><code>{v}</code> ' for v in values)
            or "Default theme"
        )

    def icon_label(value):
        if not value:
            return "Default"
        if value.startswith("chrome://"):
            value = value.rsplit("/", 1)[-1].removesuffix(".svg")
        return escape(value)

    def tab_item(tab):
        url = tab_url(tab) or ""
        entries = tab.get("entries", [])
        title = tab.get("zenStaticLabel") or (entries[0].get("title") if entries else "") or url or "Untitled"
        content = escape(str(title))
        # A preview must never execute a bookmarklet or load remote images/scripts.
        if urlsplit(url).scheme in ("https", "http"):
            content = f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{content}</a>'
        return (
            f'<li class="tab"><div>{content} {badge(old_tabs.get(tab["zenSyncId"]), tab)}</div>'
            f"<small>{escape(url)}</small></li>"
        )

    def tree_html(workspace):
        folders = {f["id"]: f for f in after["folders"] if f.get("workspaceId") == workspace}
        children = {None: []}
        for folder_id in folders:
            children[folder_id] = []
        anchors = {}
        for tab in after["tabs"]:
            if tab.get("zenWorkspace") != workspace or tab.get("zenEssential"):
                continue
            parent = tab.get("groupId") if tab.get("groupId") in folders else None
            if parent:
                anchors[parent] = min(anchors.get(parent, len(positions)), positions[tab["zenSyncId"]])
            if not tab.get("zenIsEmpty"):
                children[parent].append(("tab", tab, positions[tab["zenSyncId"]]))
        for folder in folders.values():
            parent = folder.get("parentId") if folder.get("parentId") in folders else None
            children[parent].append(("folder", folder, anchors.get(folder["id"], len(positions))))

        def render(parent, ancestors=frozenset()):
            if parent in ancestors:
                raise ValueError("Cycle in preview folders")
            output = []
            for kind, item, _ in sorted(children[parent], key=lambda x: x[2]):
                if kind == "tab":
                    output.append(tab_item(item))
                else:
                    output.append(
                        f"<li><details open><summary>📁 {escape(item.get('name') or 'Untitled')} "
                        f"{badge(old_folders.get(item['id']), item)}</summary>"
                        f"{render(item['id'], ancestors | {parent})}</details></li>"
                    )
            return "<ul>" + "".join(output) + "</ul>" if output else '<p class="empty">Empty</p>'

        return render(None)

    content, seen_containers = [], set()
    for source in trees:
        workspace = next(s for s in after["spaces"] if s["uuid"] == state["spaces"][source["space_id"]])
        original = old_spaces.get(workspace["uuid"])
        old_style = (
            f"{color(original.get('theme'))}<br>Icon: {icon_label(original.get('icon'))}" if original else "New space"
        )
        new_style = f"{color(workspace.get('theme'))}<br>Icon: {icon_label(workspace.get('icon'))}"
        context = workspace.get("containerTabId", 0)
        content.append(
            f"<section><h2>{escape(source['name'])} → {escape(workspace.get('name') or 'Untitled')} "
            f"{badge(original, workspace)}</h2><p>Zen container: {context}</p>"
            f'<div class="styles"><div><h3>Current</h3>{old_style}</div>'
            f"<div><h3>After applying</h3>{new_style}</div></div>"
        )
        if context not in seen_containers:
            seen_containers.add(context)
            essentials = [t for t in after["tabs"] if t.get("zenEssential") and t.get("userContextId", 0) == context]
            if essentials:
                content.append(
                    "<h3>Shared container Essentials</h3><ul>" + "".join(tab_item(t) for t in essentials) + "</ul>"
                )
        content.append("<h3>Resulting sidebar</h3>" + tree_html(workspace["uuid"]) + "</section>")
    added, updated = report["added"], report["updated"]
    summary = "".join(
        f"<div><strong>{added[key]}</strong>{label}</div>"
        for key, label in (
            ("tabs", "new tabs"),
            ("folders", "new folders"),
            ("essentials", "new Essentials"),
            ("spaces", "new spaces"),
        )
    )
    warnings = "".join(f"<li>{escape(w)}</li>" for w in report.get("warnings", []))
    return (
        """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer"><title>Arc → Zen — preview</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif;line-height:1.5}body{max-width:1050px;margin:40px auto;padding:0 20px}
h1{margin-bottom:4px}h2{font-size:1.3rem}h3{font-size:1rem}a{color:inherit}section{border-top:1px solid #8886;margin-top:32px;padding-top:16px}
.stats,.styles{display:flex;gap:24px;flex-wrap:wrap}.stats>div{padding:16px;border:1px solid #8886;flex:1;min-width:120px}.stats strong{display:block;font-size:2rem}
.styles>div{flex:1;min-width:230px;background:#8881;padding:16px}ul{list-style:none;padding-left:24px}li{padding:5px 0}small{display:block;opacity:.65;overflow-wrap:anywhere}
.badge{font-size:11px;padding:3px 7px;border:1px solid #8887;margin-left:8px;white-space:nowrap}.add{background:#28a74530}.edit{background:#ffbd0030}.keep{opacity:.6}
.color{display:inline-block;width:24px;height:24px;border:1px solid #888;vertical-align:middle;margin-right:5px}.note,.empty{opacity:.7}summary{cursor:pointer}code{font-size:.9em}
</style><h1>Arc → Zen</h1><p>Preview only. Nothing has been applied.</p><div class="stats">"""
        + summary
        + "</div><p>Existing records to update: "
        + (
            f"{updated['tabs']} tabs, {updated['folders']} folders, {updated['spaces']} spaces. Mode: {escape(report['mode'])}.</p>"
            '<p class="note">This is a plan outline, not a Zen screenshot. Color swatches do not reproduce browser materials or gradient geometry. '
            "“Already present” means the record is preserved unchanged. Links open only when clicked. "
            "This file contains private tab titles and URLs; do not publish it. The plan reflects the snapshot used to create it.</p>"
        )
        + ("<details><summary>Migration notes</summary><ul>" + warnings + "</ul></details>" if warnings else "")
        + "".join(content)
        + "</html>"
    )
