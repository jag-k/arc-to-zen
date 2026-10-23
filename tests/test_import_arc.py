import copy
import ctypes
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from arc_to_zen import session as app


class TestMigration:
    def setup_method(self):
        self.base: dict[str, Any] = {
            "spaces": [
                {"name": "Work", "uuid": "work-id", "containerTabId": 2},
                {"name": "Default", "uuid": "default-id", "containerTabId": 1},
            ],
            "tabs": [
                {
                    "zenSyncId": "keep",
                    "zenWorkspace": "default-id",
                    "pinned": True,
                    "entries": [{"url": "https://keep.test"}],
                    "storage": {"keep": "untouched"},
                },
                {"zenSyncId": "work-keep", "zenWorkspace": "work-id", "pinned": False},
            ],
            "groups": [],
            "folders": [],
            "splitViewData": [],
            "lastCollected": 123,
        }
        self.tree = {
            "pinned": [
                {
                    "id": "folder",
                    "title": "Folder & <",
                    "children": [
                        {"id": "b", "title": "B", "url": "https://b.test"},
                        {
                            "id": "nested",
                            "title": "Nested",
                            "children": [{"id": "a", "title": "A", "url": 'https://a.test/?x="&y=1'}],
                        },
                        {"id": "c", "title": "C", "url": "https://c.test"},
                    ],
                },
                {"id": "empty", "title": "Empty", "children": []},
            ],
            "essentials": [{"id": "fav", "title": "Favorite", "url": "https://f.test"}],
            "unpinned": [{"id": "u", "title": "U", "url": "https://u.test"}],
        }

    def test_preservation_order_context_and_idempotence(self):
        untouched = copy.deepcopy(self.base)
        result, stats = app.migrate(self.base, self.tree, "Work")
        assert self.base == untouched
        assert stats == {"tabs": 4, "folders": 3, "essentials": 1, "placeholders": 3, "skipped": 0}
        existing = [t for t in result["tabs"] if t.get("zenSyncId") in {"keep", "work-keep"}]
        assert existing == self.base["tabs"]
        added = [t for t in result["tabs"] if t not in existing]
        assert all(t["userContextId"] == 2 for t in added)
        visible = [t for t in added if not t["zenIsEmpty"]]
        assert [t["entries"][0]["title"] for t in visible] == ["Favorite", "B", "A", "C", "U"]
        assert visible[0]["zenWorkspace"] is None
        assert visible[0]["zenEssential"]
        assert not (visible[-1]["pinned"])
        assert result["spaces"] == self.base["spaces"]
        folder, nested, empty = result["folders"]
        assert nested["parentId"] == folder["id"]
        assert nested["prevSiblingInfo"] == {"type": "tab", "id": visible[1]["zenSyncId"]}
        assert len(folder["emptyTabIds"]) == 2
        assert len(empty["emptyTabIds"]) == 1
        again, repeat = app.migrate(result, self.tree, "Work")
        assert again == result
        assert repeat["skipped"] == 5
        assert repeat["tabs"] + repeat["essentials"] + repeat["folders"] == 0

    def test_native_lz4_compatibility(self):
        library = Path("/opt/homebrew/opt/lz4/lib/liblz4.dylib")
        if not library.exists():
            pytest.skip("Native LZ4 library unavailable")
        lib = ctypes.CDLL(str(library))
        compress = lib.LZ4_compress_default
        compress.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        decompress = lib.LZ4_decompress_safe
        decompress.argtypes = compress.argtypes
        for data in (self.base, {"text": "Unicode \u03bb \U0001f9ea & <" * 10000}):
            raw = json.dumps(data, ensure_ascii=False).encode()
            buf = ctypes.create_string_buffer(len(raw) + len(raw) // 255 + 32)
            length = compress(raw, buf, len(raw), len(buf))
            assert length > 0
            assert app.decode_session(app.MAGIC + struct.pack("<I", len(raw)) + buf.raw[:length]) == data
            encoded = app.encode_session(data)
            size = struct.unpack_from("<I", encoded, 8)[0]
            out = ctypes.create_string_buffer(size)
            assert decompress(encoded[12:], out, len(encoded) - 12, size) == size
            assert json.loads(out.raw) == data

    def test_bad_sessions(self):
        for bad in (
            b"",
            app.MAGIC + struct.pack("<I", 20) + b"\xf0",
            app.MAGIC + struct.pack("<I", 20) + b"\x00\x00\x00",
        ):
            with pytest.raises(ValueError, match=r"Mozilla|Truncated|Invalid"):
                app.decode_session(bad)
        base = copy.deepcopy(self.base)
        base["spaces"][0]["containerTabId"] = -1
        with pytest.raises(ValueError, match="container"):
            app.migrate(base, self.tree, "Work")

    def test_arc_dictionary_order_is_ignored(self):
        def node(id, children, data):
            return {"id": id, "childrenIds": children, "data": data, "title": id}

        items = [
            node("a", [], {"tab": {"savedURL": "https://a.test"}}),
            node("root", ["b", "a"], {"itemContainer": {}}),
            node("b", [], {"tab": {"savedURL": "https://b.test"}}),
        ]
        space = {
            "id": "space-id",
            "title": "Work",
            "profile": {"default": True},
            "newContainerIDs": [{"pinned": {}}, "root"],
        }
        data = {
            "sidebar": {
                "containers": [
                    {"global": {}},
                    {"spaces": ["space-id", space], "items": items, "topAppsContainerIDs": []},
                ]
            }
        }
        assert [n["id"] for n in app.arc_space(data, "Work")["pinned"]] == ["b", "a"]
        items[0]["childrenIds"] = ["root"]
        with pytest.raises(ValueError, match="tab with children"):
            app.arc_space(data, "Work")

    def test_atomic_apply_backup_rollback_and_change_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / ".parentlock").touch()
            raw = app.encode_session(self.base)
            converted = app.encode_session(app.migrate(self.base, self.tree, "Work")[0])
            (profile / app.SESSION).write_bytes(raw)
            with patch.object(app.subprocess, "run") as run:
                run.return_value.returncode = 1
                backup = app.apply_session(profile, raw, converted, {})
                assert (backup / app.SESSION).read_bytes() == raw
                assert (profile / app.SESSION).read_bytes() == converted
                (profile / app.SESSION).write_bytes(app.encode_session({"changed": True}))
                with pytest.raises(ValueError, match="changed since"):
                    app.restore(profile, backup)
                (profile / app.SESSION).write_bytes(converted)
                app.restore(profile, backup)
                assert (profile / app.SESSION).read_bytes() == raw
                run.return_value.returncode = 0
                with pytest.raises(ValueError, match="Zen is running"):
                    app.apply_session(profile, raw, converted, {})
                run.return_value.returncode = 1
                (profile / app.SESSION).write_bytes(b"changed")
                with pytest.raises(ValueError, match="changed during"):
                    app.apply_session(profile, raw, converted, {})

    def test_profile_lock_held_by_another_process(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / ".parentlock").touch()
            command = (
                "import fcntl,sys; "
                'f=open(sys.argv[1],"r+b"); '
                "fcntl.lockf(f,fcntl.LOCK_EX); "
                'print("locked",flush=True); sys.stdin.read(1)'
            )
            with subprocess.Popen(
                [sys.executable, "-c", command, str(profile / ".parentlock")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            ) as child:
                try:
                    assert child.stdout is not None
                    assert child.stdout.readline().strip() == "locked"
                    with patch.object(app.subprocess, "run") as run:
                        run.return_value.returncode = 1
                        with pytest.raises(ValueError, match="locked"), app.closed_profile(profile):
                            pytest.fail("Locked profile was accepted")
                finally:
                    child.communicate("\n", timeout=5)
