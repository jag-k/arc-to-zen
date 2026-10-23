import contextlib
import copy
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from arc_to_zen import arc_data, cli
from arc_to_zen.preview import html_preview
from arc_to_zen.session import SESSION, decode_session, encode_session
from arc_to_zen.transactions import commit, json_bytes, rollback_plan, undo_session
from arc_to_zen.zen_plan import STATE_FILE, build_plan, validate


def base():
    return {
        "tabs": [],
        "folders": [],
        "groups": [],
        "spaces": [
            {"name": "Work", "uuid": "work", "containerTabId": 2, "icon": "old", "theme": {"keep": True}},
            {"name": "Other", "uuid": "other", "containerTabId": 2},
        ],
        "splitViewData": [],
    }


def contexts():
    return {
        "version": 8,
        "lastUserContextId": 2,
        "identities": [{"userContextId": 2, "name": "Work", "public": True, "color": "orange", "icon": "briefcase"}],
    }


def tree(name="Work", source="work-arc", profile="profile-1"):
    return {
        "name": name,
        "space_id": source,
        "profile_key": profile,
        "profile_name": "Profile 1",
        "pinned": [
            {
                "id": "f-" + source,
                "title": "Folder",
                "children": [
                    {"id": "a-" + source, "title": "A", "url": "https://a.test"},
                    {"id": "nested-" + source, "title": "Nested", "children": []},
                ],
            }
        ],
        "unpinned": [],
        "essentials": [{"id": "shared", "title": "Favorite", "url": "https://f.test"}],
        "appearance": {"icon": "🧪", "theme": {"type": "gradient", "gradientColors": []}},
        "warnings": [],
        "skipped_items": [],
    }


class TestPlan:
    def test_shared_essentials_across_spaces_and_legacy_ids(self):
        session, ctx = base(), contexts()
        result, _, state, report = build_plan(session, ctx, {}, [tree(), tree("Other", "other-arc")])
        assert report["added"]["essentials"] == 1
        assert sum(t["zenEssential"] for t in result["tabs"]) == 1
        again, _, _, report = build_plan(result, ctx, state, [tree(), tree("Other", "other-arc")])
        assert again == result
        assert all(v == 0 for v in report["added"].values())

    def test_new_spaces_share_profile_container_and_support_default_zero(self):
        session = base()
        session["spaces"] = []
        result, ctx, _state, report = build_plan(session, contexts(), {}, [tree(), tree("Other", "other-arc")])
        assert len({s["containerTabId"] for s in result["spaces"]}) == 1
        assert len(ctx["identities"]) == 2
        assert report["added"]["spaces"] == 2
        default = base()
        default["spaces"][0]["containerTabId"] = 0
        imported, _, _, _ = build_plan(default, contexts(), {}, [tree()])
        assert all(t["userContextId"] == 0 for t in imported["tabs"])

    def test_invalid_container_and_profile_mappings_fail(self):
        invalid = base()
        invalid["spaces"][0]["containerTabId"] = 999
        with pytest.raises(ValueError, match="container"):
            build_plan(invalid, contexts(), {}, [tree()])
        with pytest.raises(ValueError, match=r"different Arc profiles|share"):
            build_plan(base(), contexts(), {}, [tree(), tree("Other", "other", "another-profile")])
        with pytest.raises(ValueError, match="container"):
            build_plan(base(), contexts(), {}, [tree()], container_map={"profile-1": 999})

    def test_new_space_before_existing_peer_reuses_existing_container(self):
        result, ctx, _, _ = build_plan(base(), contexts(), {}, [tree("New", "new-source"), tree()])
        assert len(ctx["identities"]) == 1
        assert all(s["containerTabId"] == 2 for s in result["spaces"])

    def test_add_new_nested_items_order_and_update_preserves_runtime(self):
        first = tree()
        initial, ctx, state, _ = build_plan(base(), contexts(), {}, [first])
        actual_tab = next(t for t in initial["tabs"] if t.get("zenStaticLabel") == "A")
        actual_tab["storage"] = {"private": "preserved"}
        changed = copy.deepcopy(first)
        changed["pinned"][0]["children"].insert(0, {"id": "b", "title": "B", "url": "https://b.test"})
        added, _, _, _ = build_plan(initial, ctx, state, [changed])
        assert [t["zenStaticLabel"] for t in added["tabs"] if t.get("zenStaticLabel") in ("A", "B")] == ["B", "A"]
        changed["pinned"][0]["children"][1]["title"] = "Renamed"
        updated, _, _, _ = build_plan(added, ctx, state, [changed], mode="update")
        renamed = next(t for t in updated["tabs"] if t.get("zenStaticLabel") == "Renamed")
        assert renamed["storage"] == {"private": "preserved"}
        assert initial["spaces"][1] == base()["spaces"][1]

    def test_manual_matches_use_folder_path_not_url_alone(self):
        first = tree()
        original, ctx, _, _ = build_plan(base(), contexts(), {}, [first])
        # Simulate the same hierarchy created by hand, without our deterministic IDs.
        alias = {f["id"]: "manual-" + f["id"] for f in original["folders"]}
        for folder in original["folders"]:
            folder["id"] = alias[folder["id"]]
            if folder.get("parentId"):
                folder["parentId"] = alias[folder["parentId"]]
            folder["emptyTabIds"] = ["manual-" + i for i in folder["emptyTabIds"]]
            prev = folder["prevSiblingInfo"]
            if prev and prev.get("id"):
                prev["id"] = "manual-" + prev["id"]
        for group in original["groups"]:
            group["id"] = alias[group["id"]]
        for tab in original["tabs"]:
            tab["zenSyncId"] = "manual-" + tab["zenSyncId"]
            if tab.get("groupId"):
                tab["groupId"] = alias[tab["groupId"]]
        adopted, _, state, report = build_plan(original, ctx, {}, [first], duplicates="skip")
        assert report["added"]["tabs"] == 0
        assert report["added"]["folders"] == 0
        again, _, _, _ = build_plan(adopted, ctx, state, [first], duplicates="skip")
        assert again == adopted
        # Same URL at the root is deliberately a separate bookmark.
        root = copy.deepcopy(first)
        root["pinned"].append({"id": "another-copy", "title": "A root", "url": "https://a.test"})
        _, _, _, report = build_plan(adopted, ctx, state, [root], duplicates="skip")
        assert report["added"]["tabs"] == 1

    def test_appearance_only_and_keep_appearance(self):
        original = base()
        result, _, _, report = build_plan(original, contexts(), {}, [tree()], appearance_only=True)
        assert not (result["tabs"])
        assert result["spaces"][0]["icon"] == "🧪"
        assert report["updated"]["spaces"] == 1
        result, _, _, _ = build_plan(original, contexts(), {}, [tree()], copy_appearance=False)
        assert result["spaces"] == original["spaces"]
        original["tabs"] = [
            {"zenSyncId": "normal", "zenWorkspace": "work", "pinned": False},
            {"zenSyncId": "pin-other", "zenWorkspace": "other", "pinned": True},
        ]
        result, _, _, _ = build_plan(original, contexts(), {}, [tree()], appearance_only=True)
        assert result["tabs"] == original["tabs"]

    def test_selective_rollback_after_browser_saved_and_user_added_tabs(self):
        original = base()
        imported, ctx, _, _ = build_plan(original, contexts(), {}, [tree()])
        live = copy.deepcopy(imported)
        live["folders"][0]["prevSiblingInfo"] = None  # Native Zen normalization of root folders.
        live["tabs"].append(
            {
                "zenSyncId": "user-tab",
                "entries": [{"url": "https://user.test"}],
                "zenWorkspace": "work",
                "userContextId": 2,
                "pinned": False,
            }
        )
        for tab in live["tabs"]:
            tab["lastAccessed"] = 999999
        undone, _warnings = undo_session(original, imported, live)
        assert [t["zenSyncId"] for t in undone["tabs"]] == ["user-tab"]
        assert undone["spaces"][0]["icon"] == "old"
        assert undone["folders"] == []
        validate(undone, ctx)

    def test_rollback_keeps_user_children_and_edited_imports(self):
        original = base()
        imported, ctx, _, _ = build_plan(original, contexts(), {}, [tree()])
        live = copy.deepcopy(imported)
        folder = live["folders"][0]
        live["tabs"].append(
            {
                "zenSyncId": "user-tab",
                "entries": [{"url": "https://user.test"}],
                "groupId": folder["id"],
                "zenWorkspace": "work",
                "userContextId": 2,
            }
        )
        edited = next(t for t in live["tabs"] if t.get("zenStaticLabel") == "A")
        edited["zenStaticLabel"] = "User edited"
        undone, conflicts = undo_session(original, imported, live)
        assert any(t.get("zenStaticLabel") == "User edited" for t in undone["tabs"])
        assert any(t["zenSyncId"] == "user-tab" for t in undone["tabs"])
        assert any(f["id"] == folder["id"] for f in undone["folders"])
        assert conflicts
        validate(undone, ctx)

    def test_transaction_and_rollback_and_midwrite_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            original = base()
            ctx = contexts()
            imported, new_ctx, state, report = build_plan(original, ctx, {}, [tree()])
            raw = {SESSION: encode_session(original), "containers.json": json_bytes(ctx), STATE_FILE: None}
            changes = {
                SESSION: encode_session(imported),
                "containers.json": json_bytes(new_ctx),
                STATE_FILE: json_bytes(state),
            }
            for name, value in raw.items():
                if value is not None:
                    (profile / name).write_bytes(value)
            with patch("arc_to_zen.transactions.closed_profile") as closed:
                closed.return_value.__enter__.return_value = None
                backup = commit(profile, raw, changes, report)
                live = decode_session((profile / SESSION).read_bytes())
                live["tabs"].append({"zenSyncId": "post-import", "entries": [{"url": "https://later.test"}]})
                (profile / SESSION).write_bytes(encode_session(live))
                originals, undo, report = rollback_plan(profile, backup)
                assert [t["zenSyncId"] for t in decode_session(undo[SESSION])["tabs"]] == ["post-import"]
                commit(profile, originals, undo, report)
                from arc_to_zen.session import atomic_write

                calls = []

                def failing(path, value):
                    if path.parent == profile:
                        calls.append(path.name)
                        if path.name == SESSION:
                            raise OSError("simulated write failure")
                    atomic_write(path, value)

                baseline = {name: (profile / name).read_bytes() for name in changes}
                with (
                    patch("arc_to_zen.transactions.atomic_write", side_effect=failing),
                    pytest.raises(OSError, match="simulated"),
                ):
                    commit(profile, baseline, {STATE_FILE: b'{"changed":true}', SESSION: b"changed"}, {})
                assert (profile / STATE_FILE).read_bytes() == baseline[STATE_FILE]
                assert (profile / SESSION).read_bytes() == baseline[SESSION]

    def test_color_emoji_and_unsupported_icon(self):
        warnings = []
        result = arc_data.appearance(
            {
                "iconType": {"emoji": "🧪"},
                "windowTheme": {
                    "background": {
                        "single": {"color": {"red": 0.22, "green": 0.16, "blue": 0.26, "colorSpace": "extendedSRGB"}}
                    }
                },
            },
            warnings,
        )
        assert result["icon"] == "🧪"
        assert result["theme"]["gradientColors"][0]["c"] == "#382942"
        assert warnings
        assert arc_data.icon({"iconType": {"icon": "people"}}, []) == arc_data.ICON_BASE + "people.svg"
        warnings = []
        assert arc_data.icon({"iconType": {"icon": "not-supported"}}, warnings) is None
        assert warnings

    def test_preferences_whitespace_and_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / "prefs.js").write_text('user_pref( "zen.workspaces.separate-essentials" , false );')
            assert not (cli.separate_essentials(profile))
            (profile / "user.js").write_text('user_pref("zen.workspaces.separate-essentials", true);')
            assert cli.separate_essentials(profile)
            (profile / "user.js").write_text('// user_pref("zen.workspaces.separate-essentials", false);')
            assert not (cli.separate_essentials(profile))  # prefs.js is still false.

    def test_arc_unsupported_report_and_metadata_only(self):
        space = {
            "id": "space",
            "title": "Work",
            "profile": {"default": True},
            "newContainerIDs": [{"pinned": {}}, "root"],
            "customInfo": {"iconType": {"emoji": "🧪"}},
        }
        data = {
            "sidebar": {
                "containers": [
                    {"global": {}},
                    {
                        "spaces": ["space", space],
                        "items": [
                            {"id": "root", "childrenIds": ["unknown"], "data": {"itemContainer": {}}},
                            {"id": "unknown", "childrenIds": [], "data": {"splitView": {}}},
                        ],
                        "topAppsContainerIDs": [],
                    },
                ]
            }
        }
        with pytest.raises(ValueError, match="unsupported"):
            arc_data.read_space(data, "Work")
        skipped = arc_data.read_space(data, "Work", unsupported="skip")
        assert len(skipped["skipped_items"]) == 1
        style = arc_data.read_space(data, "Work", metadata_only=True)
        assert style["appearance"]["icon"] == "🧪"

    def test_new_container_removed_by_selective_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            original = base()
            ctx = contexts()
            imported, new_ctx, state, report = build_plan(original, ctx, {}, [tree("New", "new", "new-profile")])
            originals = {SESSION: encode_session(original), "containers.json": json_bytes(ctx), STATE_FILE: None}
            changes = {
                SESSION: encode_session(imported),
                "containers.json": json_bytes(new_ctx),
                STATE_FILE: json_bytes(state),
            }
            for name, value in originals.items():
                if value is not None:
                    (profile / name).write_bytes(value)
            with patch("arc_to_zen.transactions.closed_profile"):
                backup = commit(profile, originals, changes, report)
            _, undone, _ = rollback_plan(profile, backup)
            assert json.loads(undone["containers.json"])["identities"] == ctx["identities"]
            assert decode_session(undone[SESSION])["spaces"] == original["spaces"]

    def test_cli_preview_is_read_only_and_untested_version_blocks_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            profile.mkdir()
            raw = encode_session(base())
            (profile / SESSION).write_bytes(raw)
            (profile / "containers.json").write_bytes(json_bytes(contexts()))
            (profile / "compatibility.ini").write_text("[Compatibility]\nLastVersion=99.0_12345\n")
            space = {
                "id": "source",
                "title": "Work",
                "profile": {"default": True},
                "newContainerIDs": [{"pinned": {}}, "root"],
                "customInfo": {"iconType": {"emoji": "🧪"}},
            }
            data = {
                "sidebar": {
                    "containers": [
                        {"global": {}},
                        {
                            "spaces": ["source", space],
                            "items": [
                                {"id": "root", "childrenIds": ["tab"], "data": {"itemContainer": {}}},
                                {
                                    "id": "tab",
                                    "childrenIds": [],
                                    "data": {"tab": {"savedURL": "https://example.test", "savedTitle": "Test"}},
                                },
                            ],
                            "topAppsContainerIDs": [],
                        },
                    ]
                }
            }
            source = root / "Arc.json"
            source.write_bytes(json_bytes(data))
            args = ["--arc-json", str(source), "--zen-profile", str(profile), "--space", "Work"]
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main([*args, "--output-dir", str(root / "preview")])
            assert (profile / SESSION).read_bytes() == raw
            assert not ((profile / STATE_FILE).exists())
            assert (root / "preview" / SESSION).exists()
            with patch.object(cli.sys, "platform", "darwin"), patch("arc_to_zen.cli.commit") as apply:
                with pytest.raises(ValueError, match="Untested Zen"):
                    cli.main([*args, "--apply"])
                apply.assert_not_called()

    def test_html_preview_escapes_titles_and_marks_actual_changes(self):
        original = base()
        source = tree()
        source["pinned"][0]["title"] = "<script>bad()</script>"
        source["pinned"][0]["children"][0]["title"] = "A & B"
        imported, _, state, report = build_plan(original, contexts(), {}, [source])
        html = html_preview(original, imported, state, [source], report)
        assert "&lt;script&gt;bad()&lt;/script&gt;" in html
        assert "<script>" not in html
        assert "A &amp; B" in html
        assert "Will be added" in html
        again, _, state, report = build_plan(imported, contexts(), state, [source])
        html = html_preview(imported, again, state, [source], report)
        assert "Already present" in html
        assert report["added"]["tabs"] == 0
