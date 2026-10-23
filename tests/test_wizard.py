"""The wizard applies the reviewed plan without repeating selections."""

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from arc_to_zen import cli, interaction
from arc_to_zen.boosts import BOOST_FILE, EXPORT_FORMAT
from arc_to_zen.session import SESSION, ProfileBusyError, ProfileChangedError, decode_session, encode_session
from arc_to_zen.transactions import commit, json_bytes
from arc_to_zen.zen_plan import STATE_FILE


@pytest.fixture
def wizard_profile(tmp_path: Path) -> tuple[list[str], Path]:
    profile = tmp_path / "zen"
    profile.mkdir()
    (profile / SESSION).write_bytes(encode_session({"tabs": [], "folders": [], "groups": [], "spaces": []}))
    (profile / "containers.json").write_bytes(json_bytes({"version": 8, "lastUserContextId": 0, "identities": []}))
    (profile / "compatibility.ini").write_text("[Compatibility]\nLastVersion=1.23.4b\n")
    (profile / ".parentlock").touch()
    source = tmp_path / "Arc.json"
    source.write_bytes(
        json_bytes(
            {
                "sidebar": {
                    "containers": [
                        {"global": {}},
                        {
                            "spaces": [
                                "work",
                                {
                                    "id": "work",
                                    "title": "Work",
                                    "profile": {"default": True},
                                    "newContainerIDs": [{"pinned": {}}, "root"],
                                },
                            ],
                            "items": [
                                {"id": "root", "childrenIds": ["tab"], "data": {"itemContainer": {}}},
                                {"id": "tab", "childrenIds": [], "data": {"tab": {"savedURL": "https://example.test"}}},
                            ],
                            "topAppsContainerIDs": [],
                        },
                    ]
                }
            }
        )
    )
    data = json.loads(source.read_bytes())
    container = data["sidebar"]["containers"][1]
    space = container["spaces"][1]
    space["newContainerIDs"].extend([{"unpinned": {}}, "unpinned-root"])
    container["topAppsContainerIDs"] = [{"default": True}, "favorites-root"]
    for root_id, tab_id, title in [
        ("unpinned-root", "unpinned-tab", "Unpinned"),
        ("favorites-root", "favorite", "Favorite"),
    ]:
        container["items"].extend(
            [
                {"id": root_id, "childrenIds": [tab_id], "data": {"itemContainer": {}}},
                {
                    "id": tab_id,
                    "childrenIds": [],
                    "data": {"tab": {"savedURL": f"https://{tab_id}.test", "savedTitle": title}},
                },
            ]
        )
    source.write_bytes(json_bytes(data))
    return ["--arc-json", str(source), "--zen-profile", str(profile), "--wizard"], profile


@pytest.fixture
def scripted(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys, "platform", "darwin")

    def configure(confirmations=(), overrides=None):
        replies = iter(confirmations)
        overrides = overrides or {}
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            if prompt.startswith("Apply this plan"):
                return next(replies)
            for prefix, value in overrides.items():
                if prompt.startswith(prefix):
                    return value(prompt) if callable(value) else value
            allowed = (
                "Features:",
                "Favorites [",
                "Existing imported items [",
                "Missing workspaces",
                "Spaces:",
                "Destination:",
                "Manually copied tabs [",
                "Unsupported sidebar items [",
                "Unsupported Boost settings [",
                "Boosts:",
                "Arc CSS variables [",
                "Palette space [",
            )
            assert prompt.startswith(allowed), prompt
            return ""

        monkeypatch.setattr("builtins.input", answer)
        return prompts

    return configure


@pytest.mark.parametrize("apply_flag", [False, True])
def test_wizard_applies_in_same_session(wizard_profile, scripted, apply_flag):
    args, profile = wizard_profile
    prompts = scripted(["yes"])
    with (
        patch("arc_to_zen.interaction.prepare_browser", return_value=True),
        patch("arc_to_zen.cli.commit", return_value=profile / "backup") as commit,
    ):
        cli.main([*args, *(["--apply"] if apply_flag else [])])
    assert sum(prompt.startswith("Features:") for prompt in prompts) == 1
    commit.assert_called_once()
    assert commit.call_args.args[0] == profile


@pytest.mark.parametrize("answer", ["", "no"])
def test_wizard_decline_keeps_profile(wizard_profile, scripted, answer):
    args, profile = wizard_profile
    before = {p.name: p.read_bytes() for p in profile.iterdir()}
    scripted([answer])
    with patch("arc_to_zen.cli.commit") as commit, patch("arc_to_zen.interaction.prepare_browser") as close:
        cli.main(args)
    commit.assert_not_called()
    close.assert_not_called()
    assert {p.name: p.read_bytes() for p in profile.iterdir()} == before


@pytest.mark.parametrize("error", [ProfileBusyError("Zen is running"), ProfileChangedError("Profile changed")])
def test_wizard_transaction_race_keeps_choices(wizard_profile, scripted, error):
    args, profile = wizard_profile
    prompts = scripted(["yes", "yes"])
    with (
        patch("arc_to_zen.interaction.prepare_browser", return_value=True),
        patch("arc_to_zen.cli.commit", side_effect=[error, profile / "backup"]) as commit,
    ):
        cli.main(args)
    assert commit.call_count == 2
    assert sum(prompt.startswith("Features:") for prompt in prompts) == 1
    assert sum(prompt.startswith("Spaces:") for prompt in prompts) == 1


@pytest.mark.parametrize("output_flag", ["--preview-only", "--preview-html", "--output-dir"])
def test_wizard_preview_outputs_do_not_offer_apply(wizard_profile, scripted, tmp_path, output_flag):
    args, _ = wizard_profile
    extra = [output_flag] if output_flag == "--preview-only" else [output_flag, str(tmp_path / "preview")]
    prompts = scripted()
    with patch("arc_to_zen.cli.commit") as commit, patch("arc_to_zen.interaction.prepare_browser") as close:
        cli.main([*args, *extra])
    assert not any(prompt.startswith("Apply this plan") for prompt in prompts)
    commit.assert_not_called()
    close.assert_not_called()


@pytest.mark.parametrize(
    ("platform", "version", "message"), [("linux", "1.23.4b", "macOS"), ("darwin", "999", "Untested Zen")]
)
def test_wizard_confirmation_cannot_bypass_compatibility(
    wizard_profile, scripted, monkeypatch, platform, version, message
):
    args, profile = wizard_profile
    (profile / "compatibility.ini").write_text(f"[Compatibility]\nLastVersion={version}\n")
    scripted()
    monkeypatch.setattr(cli.sys, "platform", platform)
    with patch("arc_to_zen.cli.commit") as commit, pytest.raises(ValueError, match=message):
        cli.main(args)
    commit.assert_not_called()


def add_user_tab(profile):
    session = decode_session((profile / SESSION).read_bytes())
    session["tabs"].append({"zenSyncId": "user-tab", "entries": [{"url": "https://user.test"}], "userContextId": 0})
    (profile / SESSION).write_bytes(encode_session(session))


@pytest.mark.parametrize("accept_updated", [True, False])
def test_wizard_rebuilds_after_browser_save(wizard_profile, scripted, accept_updated, capsys):
    args, profile = wizard_profile
    prompts = scripted(["yes", "yes" if accept_updated else "no"])

    def browser_closed(_):
        if not any(t["zenSyncId"] == "user-tab" for t in decode_session((profile / SESSION).read_bytes())["tabs"]):
            add_user_tab(profile)
        return True

    with (
        patch("arc_to_zen.interaction.prepare_browser", side_effect=browser_closed),
        patch("arc_to_zen.transactions.closed_profile"),
    ):
        cli.main(args)
    saved = decode_session((profile / SESSION).read_bytes())
    assert any(t["zenSyncId"] == "user-tab" for t in saved["tabs"])
    assert any(t.get("zenStaticLabel") for t in saved["tabs"]) == accept_updated
    assert sum(prompt.startswith("Spaces:") for prompt in prompts) == 1
    assert "Updated plan" in capsys.readouterr().out


@pytest.mark.parametrize(("features", "counts"), [("1", (1, 0)), ("2", (1, 0)), ("3", (1, 0)), ("4", (0, 0))])
def test_numbered_features_control_actual_plan(wizard_profile, scripted, features, counts):
    args, _ = wizard_profile
    scripted(["yes"], {"Features:": features})
    with patch("arc_to_zen.interaction.prepare_browser", return_value=True), patch("arc_to_zen.cli.commit") as commit:
        cli.main(args)
    changes = commit.call_args.args[2]
    result = decode_session(changes[SESSION])
    assert (len(result["tabs"]), len(result["folders"])) == counts


def test_boosts_only_wizard_needs_no_sidebar(wizard_profile, scripted, tmp_path):
    args, _ = wizard_profile
    source = tmp_path / "boosts.json"

    source.write_bytes(
        json_bytes(
            {
                "format": EXPORT_FORMAT,
                "boosts": [
                    {
                        "id": "test",
                        "title": "Example",
                        "hostPattern": "example.test",
                        "active": True,
                        "data": {"css": "body{color:red}"},
                    },
                    {
                        "id": "other",
                        "title": "Other",
                        "hostPattern": "other.test",
                        "active": False,
                        "data": {"css": "body{color:blue}"},
                    },
                ],
            }
        )
    )
    prompts = scripted(["yes"], {"Features:": "5", "Boosts:": "2"})
    with patch("arc_to_zen.interaction.prepare_browser", return_value=True), patch("arc_to_zen.cli.commit") as commit:
        cli.main([*args, "--boosts-json", str(source), "--arc-json", str(tmp_path / "missing-sidebar.json")])
    assert not any(prompt.startswith("Spaces:") for prompt in prompts)
    store = decode_session(commit.call_args.args[2][BOOST_FILE])
    assert set(store) == {"other.test"}


@pytest.mark.parametrize("answer", ["", "y", "c"])
def test_browser_closing_requires_explicit_authorization(tmp_path, monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda _: answer)
    with (
        patch(
            "arc_to_zen.interaction.closed_profile",
            side_effect=[ProfileBusyError("Zen is running"), nullcontext()],
        ),
        patch("arc_to_zen.interaction.quit_zen", return_value=True) as quit_browser,
    ):
        assert interaction.prepare_browser(tmp_path) == (answer != "c")
    assert quit_browser.call_count == (1 if answer == "y" else 0)


def test_quit_zen_uses_graceful_apple_event():
    with patch("arc_to_zen.interaction.subprocess.run") as run:
        run.return_value.returncode = 0
        assert interaction.quit_zen()
    command = run.call_args.args[0]
    assert command[:2] == ["/usr/bin/osascript", "-e"]
    assert 'if application id "app.zen-browser.zen" is running' in command[2]
    assert "to quit" in command[2]
    assert run.call_args.kwargs["timeout"] == 15


def test_numbered_choices_retry_invalid_input(monkeypatch):
    answers = iter(["99", "1,1", "1,3"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert interaction.choose_many("Features", ["a", "b", "c"], [0]) == [0, 2]


def test_disabled_pinned_feature_does_not_parse_unsupported_pinned_items(wizard_profile, scripted):
    args, _ = wizard_profile
    source = Path(args[1])
    data = json.loads(source.read_bytes())
    next(item for item in data["sidebar"]["containers"][1]["items"] if item["id"] == "tab")["data"] = {"splitView": {}}
    source.write_bytes(json_bytes(data))
    scripted(["yes"], {"Features:": "2"})
    with patch("arc_to_zen.interaction.prepare_browser", return_value=True), patch("arc_to_zen.cli.commit") as commit:
        cli.main(args)
    saved = decode_session(commit.call_args.args[2][SESSION])
    assert [t["entries"][0]["title"] for t in saved["tabs"]] == ["Unpinned"]


@pytest.mark.parametrize("failure", [OSError("unavailable"), subprocess.TimeoutExpired("osascript", 15)])
def test_quit_failure_returns_to_manual_workflow(failure):
    with patch("arc_to_zen.interaction.subprocess.run", side_effect=failure):
        assert not interaction.quit_zen()


def test_closed_browser_cannot_bypass_changed_read_only_inputs(tmp_path):

    old_state = b"{}"
    old_session = encode_session({"tabs": [], "folders": [], "groups": [], "spaces": []})
    (tmp_path / SESSION).write_bytes(old_session)
    (tmp_path / STATE_FILE).write_bytes(b'{"user":true}')
    originals = {SESSION: old_session, STATE_FILE: old_state}
    with patch("arc_to_zen.transactions.closed_profile"), pytest.raises(ProfileChangedError, match="changed"):
        commit(tmp_path, originals, {SESSION: b"new session"}, {})
    assert (tmp_path / SESSION).read_bytes() == old_session
