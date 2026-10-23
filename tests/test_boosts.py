import json
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from arc_to_zen.boosts import BOOST_FILE, EXPORT_FORMAT, convert, load_arc_boosts, palette_snapshot, plan_boosts
from arc_to_zen.firestore_proto import document, encode_varint
from arc_to_zen.leveldb_readonly import crc32c, snappy, snapshot
from arc_to_zen.session import SESSION, decode_session, encode_session
from arc_to_zen.transactions import commit, json_bytes, rollback_plan


def pb(number, data):
    return encode_varint(number * 8 + 2) + encode_varint(len(data)) + data


def boost_document(identifier, owner="owner", css="body { color: red }"):
    fields = {
        "creatorID": owner,
        "hostPattern": "example.test",
        "isActive": "true",
        "title": "Sample",
        "data": json.dumps({"css": css, "zaps": [".ad"]}),
    }
    name = "projects/test/databases/(default)/documents/boosts/" + identifier
    doc = pb(1, name.encode())
    for key, value in fields.items():
        doc += pb(2, pb(1, key.encode()) + pb(2, pb(17, value.encode())))
    return pb(2, doc)


def masked_crc(raw):
    value = crc32c(raw)
    return (((value >> 15) | (value << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def log_record(raw):
    return struct.pack("<IHB", masked_crc(b"\x01" + raw), len(raw), 1) + raw


def string(value):
    return encode_varint(len(value)) + value


def table(rows):
    def block(entries):
        raw = b"".join(b"\0" + encode_varint(len(k)) + encode_varint(len(v)) + k + v for k, v in entries)
        return raw + struct.pack("<II", 0, 1)

    def trailer(raw):
        return raw + b"\0" + struct.pack("<I", masked_crc(raw + b"\0"))

    internal = [(key + struct.pack("<Q", (sequence << 8) | 1), value) for key, sequence, value in rows]
    data = block(internal)
    encoded = trailer(data)
    meta = block([])
    meta_handle = encode_varint(len(encoded)) + encode_varint(len(meta))
    encoded += trailer(meta)
    index = block([(internal[-1][0], encode_varint(0) + encode_varint(len(data)))])
    footer = meta_handle + encode_varint(len(encoded)) + encode_varint(len(index))
    encoded += trailer(index)
    return encoded + footer.ljust(40, b"\0") + bytes.fromhex("57fb808b247547db")


def source(**data):
    return {
        "id": "source-id",
        "title": "Sample",
        "hostPattern": "example.test",
        "active": True,
        "data": {"css": "body { --migrated: yes }", "zaps": [".ad"], **data},
    }


class TestBoosts:
    def test_crc_and_snappy_vectors(self):
        assert crc32c(b"123456789") == 3808858755
        assert snappy(b"\x05\x10hello") == b"hello"
        assert snappy(b"\x08\x00a\x1a\x01\x00") == b"aaaaaaaa"
        with pytest.raises(ValueError, match="Snappy"):
            snappy(b"\x05\x02\x00\x00")

    def test_manifest_live_files_tombstones_and_no_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            data = table([(b"a", 5, b"old"), (b"b", 5, b"delete-me")])
            (path / "000002.ldb").write_bytes(data)
            (path / "000004.ldb").write_bytes(table([(b"a", 999, b"orphan")]))
            smallest = b"a" + struct.pack("<Q", 5 << 8 | 1)
            largest = b"b" + struct.pack("<Q", 5 << 8 | 1)
            manifest = b"\x02\x03\x07\x00\x02" + encode_varint(len(data)) + string(smallest) + string(largest)
            (path / "MANIFEST-000001").write_bytes(log_record(manifest))
            (path / "CURRENT").write_text("MANIFEST-000001\n")
            batch = struct.pack("<QI", 6, 2) + b"\x01" + string(b"a") + string(b"new") + b"\0" + string(b"b")
            (path / "000003.log").write_bytes(log_record(batch))
            before = {p.name: p.read_bytes() for p in path.iterdir()}
            assert snapshot(path) == {b"a": b"new"}
            assert {p.name: p.read_bytes() for p in path.iterdir()} == before
            corrupted = bytearray(data)
            corrupted[5] ^= 1
            (path / "000002.ldb").write_bytes(corrupted)
            with pytest.raises(ValueError, match="checksum"):
                snapshot(path)

    def test_firestore_decoding_and_current_account_filter(self):
        raw = boost_document("one")
        name, fields = document(raw)
        assert name.endswith("/boosts/one")
        assert json.loads(fields["data"])["zaps"] == [".ad"]
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "auth.json"
            auth.write_text('{"session":{"userID":"owner"}}')
            rows = {
                b"remote_document\0boosts\0one": raw,
                b"remote_document\0boosts\0two": boost_document("two", "other-owner"),
            }
            with patch("arc_to_zen.boosts.snapshot", return_value=rows):
                export = load_arc_boosts(Path(directory), auth)
            assert [b["id"] for b in export["boosts"]] == ["one"]
            assert "creatorID" not in export["boosts"][0]

    def test_native_css_zaps_font_and_text(self):
        entry, css, issues, usable = convert(
            source(font={"family": "Helvetica Neue"}, textZoom={"level": 150}, textTransform="capitalize")
        )
        assert entry["boostData"]["fontFamily"] == '"Helvetica Neue"'
        assert entry["boostData"]["sizeOverride"] == 1.5
        assert entry["boostData"]["zapSelectors"] == [".ad"]
        assert usable
        assert not (issues)
        assert "customCSS" not in entry["boostData"]
        assert css
        entry, _, _, _ = convert(source(font={"family": "monospace"}))
        assert entry["boostData"]["fontFamily"] == "monospace"

    def test_unsupported_js_scopes_palette_and_no_automatic_partial(self):
        export = {"format": EXPORT_FORMAT, "boosts": [source(js="document.body.remove()")], "omitted": []}
        store, files, report = plan_boosts({}, export, lambda _: None)
        assert store == {}
        assert not (files)
        assert report["skipped"] == 1
        store, files, report = plan_boosts({}, export, lambda _: None, allow_partial=True)
        assert report["added"] == 1
        assert "document.body.remove" not in json.dumps(store)
        assert "document.body.remove" not in b"".join(v or b"" for v in files.values()).decode()
        for pattern in ("*.example.test", "https://example.test/path", "example.test:8080", "*"):
            item = source()
            item["hostPattern"] = pattern
            _, _, report = plan_boosts({}, {"boosts": [item]}, lambda _: None)
            assert report["skipped"] == 1
        original_css = "body {color:var(--arc-palette-title)}"
        _, files, report = plan_boosts({}, {"boosts": [source(css=original_css)]}, lambda _: None)
        assert report["added"] == 1
        assert report["skipped"] == 0
        assert next(iter(files.values())).decode() == original_css

    def test_activation_conflicts_and_repeated_import(self):
        export = {"boosts": [source()]}
        first, css, report = plan_boosts({}, export, lambda _: None)
        assert report["activated"] == 1
        second, changes, report = plan_boosts(first, export, lambda p: css.get(p))
        assert first == second
        assert report["existing"] == 1
        assert not (changes)
        existing = {"example.test": {"activeBoostId": "manual", "boostEntries": {"manual": {"boostData": {}}}}}
        store, _, report = plan_boosts(existing, export, lambda _: None)
        assert store["example.test"]["activeBoostId"] == "manual"
        other = {**source(), "id": "another-source"}
        store, _, report = plan_boosts({}, {"boosts": [source(), other]}, lambda _: None)
        assert store["example.test"]["activeBoostId"] is None

    def test_palette_snapshot_is_explicit_and_preserves_js_limit(self):
        color = {"red": 0.2, "green": 0.1, "blue": 0.3, "alpha": 1}
        space = {
            "id": "work",
            "title": "Work",
            "customInfo": {
                "windowTheme": {
                    "semanticColorPalette": {"appearanceBased": {"light": {"title": color}, "dark": {"title": color}}},
                    "background": {"color": color},
                }
            },
        }
        data = {"sidebar": {"containers": [{"global": {}}, {"spaces": ["work", space]}]}}
        palette = palette_snapshot(data, "Work")
        _store, files, report = plan_boosts(
            {}, {"boosts": [source(css="body{color:var(--arc-palette-title)}")]}, lambda _: None, palette=palette
        )
        assert report["added"] == 1
        assert "@media (prefers-color-scheme: dark)" in next(iter(files.values())).decode()
        assert any("static snapshot" in w for w in report["warnings"])

    def test_transaction_css_sidecars_rollback_and_user_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            session = {"tabs": [], "folders": [], "groups": [], "spaces": []}
            contexts = {"version": 8, "lastUserContextId": 0, "identities": []}
            (profile / SESSION).write_bytes(encode_session(session))
            (profile / "containers.json").write_bytes(json_bytes(contexts))
            (profile / BOOST_FILE).write_bytes(encode_session({}))
            store, css, report = plan_boosts({}, {"boosts": [source()]}, lambda _: None)
            original = {BOOST_FILE: (profile / BOOST_FILE).read_bytes(), **dict.fromkeys(css)}
            changes = {BOOST_FILE: encode_session(store), **css}
            with patch("arc_to_zen.transactions.closed_profile"):
                backup = commit(profile, original, changes, report)
            _, undo, report = rollback_plan(profile, backup)
            assert decode_session(undo[BOOST_FILE]) == {}
            assert undo[next(iter(css))] is None
            assert undo[SESSION] == (profile / SESSION).read_bytes()
            with patch("arc_to_zen.transactions.closed_profile"):
                original, undo, report = rollback_plan(profile, backup)
                commit(profile, original, undo, report)
            assert not ((profile / next(iter(css))).exists())
            # Recreate the import, then preserve a CSS edit during selective rollback.
            original = {BOOST_FILE: (profile / BOOST_FILE).read_bytes(), **dict.fromkeys(css)}
            with patch("arc_to_zen.transactions.closed_profile"):
                backup = commit(profile, original, changes, {})
            path = profile / next(iter(css))
            path.write_text("/* user edit */ body {color:green}")
            _, undo, report = rollback_plan(profile, backup)
            assert decode_session(undo[BOOST_FILE])
            assert str(path.relative_to(profile)) not in undo
            assert report["warnings"]
