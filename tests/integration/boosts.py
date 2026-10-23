#!/usr/bin/env python3
"""Test native Zen Boost CSS/zaps against a local page in a NEW synthetic profile."""

import argparse
import http.server
import threading
import time
import uuid
from pathlib import Path

from arc_to_zen.boosts import BOOST_FILE, palette_snapshot, plan_boosts, rollback_boosts
from arc_to_zen.session import SESSION, decode_session, encode_session
from arc_to_zen.transactions import json_bytes
from tests.integration.sidebar import run_browser


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<!doctype html><html><body><p id="zap">Hidden by the imported zap</p><p>Local Boost test</p></body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True, help="NEW directory for synthetic test data")
    parser.add_argument("--zen-binary", type=Path, default=Path("/Applications/Zen.app/Contents/MacOS/zen"))
    args = parser.parse_args(argv)
    args.artifacts.mkdir(mode=0o700)
    profile = args.artifacts / "test-profile"
    profile.mkdir(mode=0o700)
    session = {
        "tabs": [],
        "groups": [],
        "folders": [],
        "spaces": [
            {
                "uuid": "{" + str(uuid.uuid4()) + "}",
                "name": "Test",
                "containerTabId": 0,
                "icon": "",
                "theme": {"type": "gradient", "gradientColors": [], "opacity": 1, "texture": 0},
            }
        ],
        "splitViewData": [],
    }
    (profile / SESSION).write_bytes(encode_session(session))
    (profile / "containers.json").write_bytes(json_bytes({"version": 8, "lastUserContextId": 0, "identities": []}))
    color = {"red": 0.2, "green": 0.1, "blue": 0.3, "alpha": 1}
    space = {
        "id": "test",
        "title": "Test",
        "customInfo": {
            "windowTheme": {
                "semanticColorPalette": {"appearanceBased": {"light": {"title": color}, "dark": {"title": color}}},
                "background": {"color": color},
            }
        },
    }
    palette = palette_snapshot({"sidebar": {"containers": [{"global": {}}, {"spaces": ["test", space]}]}}, "Test")
    export = {
        "boosts": [
            {
                "id": "synthetic-boost",
                "title": "Synthetic CSS and zaps",
                "hostPattern": "127.0.0.1",
                "active": True,
                "data": {
                    "css": "body { --migrated: yes; color:var(--arc-palette-title) !important; }",
                    "font": {"family": "monospace"},
                    "zaps": ["#zap"],
                },
            }
        ]
    }
    store, files, _ = plan_boosts({}, export, lambda _: None, palette=palette)
    (profile / BOOST_FILE).write_bytes(encode_session(store))
    for name, value in files.items():
        (profile / name).parent.mkdir(mode=0o700, exist_ok=True)
        (profile / name).write_bytes(value or b"")
    before = {BOOST_FILE: encode_session({}), **dict.fromkeys(files)}
    after = {BOOST_FILE: encode_session(store), **files}
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/"

        def inspect(client):
            client.script("Services.io.offline = false; return true;")
            client.call("Marionette:SetContext", {"value": "content"})
            client.call("WebDriver:Navigate", {"url": url})
            end = time.monotonic() + 10
            while True:
                result = client.script("""
                    return {marker:getComputedStyle(document.body).getPropertyValue('--migrated').trim(),
                            color:getComputedStyle(document.body).color,
                            font:getComputedStyle(document.querySelector('p:last-of-type')).fontFamily,
                            zap:getComputedStyle(document.getElementById('zap')).display};
                """)
                if result["marker"] == "yes" or time.monotonic() > end:
                    break
                time.sleep(0.1)
            client.call("Marionette:SetContext", {"value": "chrome"})
            return result

        first = run_browser(args.zen_binary, profile, args.artifacts / "zen.log", probe=inspect)["probe"]
        assert first["marker"] == "yes", first
        assert first["color"] == "rgb(51, 26, 76)", first
        assert first["zap"] == "none", first
        assert "monospace" in first["font"], first
        runtime = decode_session((profile / BOOST_FILE).read_bytes())
        _, changes, report = plan_boosts(
            runtime,
            export,
            lambda name: (profile / name).read_bytes() if (profile / name).exists() else None,
            palette=palette,
        )
        assert report["existing"] == 1, report
        assert not changes, report
        current = {name: (profile / name).read_bytes() if (profile / name).exists() else None for name in after}
        undone, conflicts = rollback_boosts(profile, before, after, current)
        assert not conflicts, conflicts
        for name, value in undone.items():
            if value is None:
                (profile / name).unlink(missing_ok=True)
            else:
                (profile / name).write_bytes(value)

        # Do not wait for a marker that must no longer be present.
        def inspect_after(client):
            client.script("Services.io.offline = false; return true;")
            client.call("Marionette:SetContext", {"value": "content"})
            client.call("WebDriver:Navigate", {"url": url})
            result = client.script(
                "return {marker:getComputedStyle(document.body).getPropertyValue('--migrated').trim(),zap:getComputedStyle(document.getElementById('zap')).display};"
            )
            client.call("Marionette:SetContext", {"value": "chrome"})
            return result

        final = run_browser(args.zen_binary, profile, args.artifacts / "zen.log", probe=inspect_after)["probe"]
        assert final["marker"] == "", final
        assert final["zap"] != "none", final
        server.shutdown()
    (args.artifacts / "result.json").write_bytes(json_bytes({"passed": True, "first": first, "after_rollback": final}))
    print(
        "PASS: real Zen applied native CSS, zap selectors, font and static Arc palette to a local page; repeated import and rollback passed."
    )


if __name__ == "__main__":
    main()
