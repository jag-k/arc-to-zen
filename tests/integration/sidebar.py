#!/usr/bin/env python3
"""Opt-in browser integration test. Only synthetic data in a NEW temporary profile."""

import argparse
import contextlib
import copy
import json
import socket
import subprocess
import time
import uuid
from pathlib import Path

from arc_to_zen.session import SESSION, decode_session, encode_session
from arc_to_zen.transactions import json_bytes, undo_session
from arc_to_zen.zen_plan import build_plan, validate


class Marionette:
    def __init__(self, port):
        self.connection = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.connection.settimeout(20)
        self.counter = 0
        self.receive()
        self.call("WebDriver:NewSession", {"capabilities": {}})
        self.call("Marionette:SetContext", {"value": "chrome"})

    def receive(self):
        digits = bytearray()
        while True:
            char = self.connection.recv(1)
            if not char:
                raise EOFError("Marionette closed")
            if char == b":":
                break
            digits.extend(char)
        size = int(digits)
        data = bytearray()
        while len(data) < size:
            block = self.connection.recv(size - len(data))
            if not block:
                raise EOFError("Truncated Marionette response")
            data.extend(block)
        return json.loads(data)

    def call(self, command, params=None):
        self.counter += 1
        raw = json.dumps([0, self.counter, command, params or {}]).encode()
        self.connection.sendall(str(len(raw)).encode() + b":" + raw)
        while True:
            response = self.receive()
            if response[0] == 1 and response[1] == self.counter:
                if response[2]:
                    raise ValueError(response[2])
                return response[3]

    def script(self, script, args=None):
        response = self.call(
            "WebDriver:ExecuteScript",
            {"script": script, "args": args or [], "newSandbox": True, "sandbox": "system", "filename": __file__},
        )
        return response.get("value", response) if isinstance(response, dict) else response


def run_browser(binary, profile, log, *, add_user_tab=False, probe=None):
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    prefs = {
        "marionette.port": port,
        "marionette.enabled": True,
        "zen.welcome-screen.seen": True,
        "browser.shell.checkDefaultBrowser": False,
        "browser.startup.page": 3,
        "browser.startup.homepage_override.mstone": "ignore",
        "browser.sessionstore.restore_on_demand": True,
        "browser.sessionstore.restore_pinned_tabs_on_demand": True,
        "zen.workspaces.continue-where-left-off": True,
        "network.manage-offline-status": False,
        "network.offline": True,
        "app.update.auto": False,
        "datareporting.policy.dataSubmissionEnabled": False,
        "toolkit.telemetry.enabled": False,
    }
    (profile / "user.js").write_text(
        "\n".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in prefs.items())
    )
    process = None
    client = None
    with log.open("ab") as output:
        process = subprocess.Popen(
            [
                str(binary),
                "-headless",
                "-no-remote",
                "--remote-allow-system-access",
                "-profile",
                str(profile),
                "-marionette",
            ],
            stdout=output,
            stderr=output,
        )
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ValueError(f"Zen exited early ({process.returncode}); log: {log}")
                try:
                    client = Marionette(port)
                    break
                except (ConnectionError, OSError):
                    time.sleep(0.2)
            if client is None:
                raise ValueError(f"Zen did not start Marionette; log: {log}")
            while time.monotonic() < deadline:
                ready = client.script(
                    "return Boolean(window.gZenWorkspaces?.getWorkspaces && window.gBrowser?.tabs && window.gZenThemePicker);"
                )
                if ready:
                    break
                time.sleep(0.2)
            else:
                raise ValueError("Zen chrome UI did not initialize")
            snapshot = client.script("""
                Services.io.offline = true;
                return {
                  tabs: Array.from(document.querySelectorAll('tab.tabbrowser-tab')).map(t => ({id: t.id,
                    empty: t.hasAttribute('zen-empty-tab'), essential: t.hasAttribute('zen-essential'),
                    workspace: t.getAttribute('zen-workspace-id'), context: t.userContextId,
                    folders: (() => { const out = []; let p = t.parentElement?.closest('zen-folder');
                      while (p) {out.unshift(p.label); p = p.parentElement?.closest('zen-folder');} return out; })()})),
                  folders: Array.from(document.querySelectorAll('zen-folder')).map(f => ({id:f.id, name:f.label})),
                  spaces: gZenWorkspaces.getWorkspaces().map(s => ({name:s.name, uuid:s.uuid,
                    theme:s.theme, icon:s.icon,
                    gradient:gZenThemePicker.getGradientForWorkspace(s).gradient}))
                };
            """)
            if probe:
                snapshot["probe"] = probe(client)
            if add_user_tab:
                user_tab = client.script("""
                    const t = gBrowser.addTrustedTab('about:robots', {userContextId:2, inBackground:true});
                    t.setAttribute('zen-workspace-id', gZenWorkspaces.getWorkspaces().find(s => s.name === 'Work').uuid);
                    gBrowser.pinTab(t);
                    return t.id;
                """)
                snapshot["user_tab_id"] = user_tab
                # Let native tab/session event handlers finish before a graceful quit.
                time.sleep(1)
            # Graceful shutdown saves the profile, unlike killing a screenshot process.
            with contextlib.suppress(EOFError, OSError):
                client.call("Marionette:Quit", {"flags": ["eAttemptQuit"]})
            process.wait(timeout=20)
            return snapshot
        finally:
            if client:
                client.connection.close()
            if process and process.poll() is None:
                # Only terminate the child process started by this test.
                process.terminate()
                process.wait(timeout=10)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zen-binary", type=Path, default=Path("/Applications/Zen.app/Contents/MacOS/zen"))
    parser.add_argument("--artifacts", type=Path, required=True, help="NEW directory for synthetic profiles/logs")
    args = parser.parse_args(argv)
    args.artifacts.mkdir(mode=0o700)
    profile = args.artifacts / "test-profile"
    profile.mkdir(mode=0o700)
    work, other = ("{" + str(uuid.uuid4()) + "}" for _ in range(2))
    original = {
        "tabs": [],
        "folders": [],
        "groups": [],
        "spaces": [
            {
                "name": "Work",
                "uuid": work,
                "containerTabId": 2,
                "icon": "",
                "theme": {"type": "gradient", "gradientColors": [], "opacity": 1, "texture": 0},
            },
            {
                "name": "Other",
                "uuid": other,
                "containerTabId": 2,
                "icon": "",
                "theme": {"type": "gradient", "gradientColors": [], "opacity": 1, "texture": 0},
            },
        ],
        "splitViewData": [],
        "lastCollected": int(time.time() * 1000),
    }
    contexts = {
        "version": 8,
        "lastUserContextId": 2,
        "identities": [{"userContextId": 2, "public": True, "name": "Work", "color": "purple", "icon": "briefcase"}],
    }
    source = {
        "name": "Work",
        "space_id": "arc-work",
        "profile_key": "shared-profile",
        "profile_name": "Work",
        "pinned": [
            {
                "id": "folder",
                "title": "Parent",
                "children": [
                    {"id": "before", "title": "Before", "url": "https://example.test/before"},
                    {
                        "id": "nested",
                        "title": "Nested",
                        "children": [{"id": "inside", "title": "Inside", "url": "https://example.test/inside"}],
                    },
                    {"id": "after", "title": "After", "url": "https://example.test/after"},
                ],
            }
        ],
        "unpinned": [],
        "essentials": [{"id": "favorite", "title": "Favorite", "url": "https://example.test/favorite"}],
        "appearance": {
            "icon": "🧪",
            "theme": {
                "type": "gradient",
                "opacity": 1,
                "texture": 0.4375,
                "gradientColors": [
                    {
                        "c": "#382a42",
                        "isCustom": True,
                        "isPrimary": True,
                        "algorithm": "floating",
                        "lightness": 50,
                        "position": {"x": 160, "y": 160},
                        "type": "explicit-lightness",
                    }
                ],
            },
        },
        "warnings": [],
        "skipped_items": [],
    }
    second = {**copy.deepcopy(source), "name": "Other", "space_id": "arc-other", "pinned": []}
    third = {
        **copy.deepcopy(source),
        "name": "Created",
        "space_id": "arc-created",
        "profile_key": "separate-profile",
        "profile_name": "Separate",
        "pinned": [{"id": "new-context-tab", "title": "New context", "url": "https://example.test/new-context"}],
    }
    sources = [source, second, third]
    imported, contexts, state, report = build_plan(original, contexts, {}, sources)
    (profile / SESSION).write_bytes(encode_session(imported))
    (profile / "containers.json").write_bytes(json_bytes(contexts))
    snapshot = run_browser(args.zen_binary, profile, args.artifacts / "zen.log", add_user_tab=True)
    (args.artifacts / "first-snapshot.json").write_bytes(json_bytes(snapshot))
    expected = [
        t["zenSyncId"]
        for t in imported["tabs"]
        if not t["zenIsEmpty"] and not t["zenEssential"] and t.get("zenWorkspace") == work
    ]
    actual = [t["id"] for t in snapshot["tabs"] if t["id"] in set(expected)]
    assert actual == expected, (actual, expected)
    paths = {t["id"]: t["folders"] for t in snapshot["tabs"] if t["id"] in set(expected)}
    assert list(paths.values()) == [["Parent"], ["Parent", "Nested"], ["Parent"]], paths
    essentials = [t for t in snapshot["tabs"] if t["essential"]]
    assert len(essentials) == 2, essentials
    assert sorted(t["context"] for t in essentials) == [2, 3], essentials
    created = next(s for s in snapshot["spaces"] if s["name"] == "Created")
    assert any(t["workspace"] == created["uuid"] and t["context"] == 3 and not t["empty"] for t in snapshot["tabs"])
    theme = next(s for s in snapshot["spaces"] if s["name"] == "Work")
    assert theme["icon"] == "🧪", theme
    assert theme["gradient"] == "#382a42", theme
    runtime = decode_session((profile / SESSION).read_bytes())
    user_tab_id = next(
        (t["zenSyncId"] for t in runtime["tabs"] if any(e.get("url") == "about:robots" for e in t.get("entries", []))),
        None,
    )
    assert user_tab_id, "Native user tab was not saved"
    repeated, _, _, report = build_plan(runtime, json.loads((profile / "containers.json").read_text()), state, sources)
    assert all(v == 0 for v in report["added"].values()), report
    assert repeated == runtime, "Repeat import changed browser-normalized session"
    second_snapshot = run_browser(args.zen_binary, profile, args.artifacts / "zen.log")
    assert [t["id"] for t in second_snapshot["tabs"] if t["id"] in set(expected)] == expected
    saved = decode_session((profile / SESSION).read_bytes())
    undone, conflicts = undo_session(original, imported, saved)
    assert any(t["zenSyncId"] == user_tab_id for t in undone["tabs"]), "User tab was lost"
    imported_ids = {t["zenSyncId"] for t in imported["tabs"]}
    assert not any(t["zenSyncId"] in imported_ids for t in undone["tabs"]), conflicts
    assert not undone["folders"], conflicts
    validate(undone, json.loads((profile / "containers.json").read_text()))
    (profile / SESSION).write_bytes(encode_session(undone))
    final = run_browser(args.zen_binary, profile, args.artifacts / "zen.log")
    assert any(t["id"] == user_tab_id for t in final["tabs"])
    assert not any(t["id"] in imported_ids for t in final["tabs"])
    (args.artifacts / "result.json").write_bytes(
        json_bytes({"passed": True, "cycles": 3, "snapshot": snapshot, "rollback_conflicts": conflicts})
    )
    print(
        "PASS: real Zen import, folder nesting/order, shared Essentials, RGB theme/icon, restart, idempotence and selective rollback preserving a user tab."
    )


if __name__ == "__main__":
    main()
