# Arc → Zen

Move your Arc sidebar into Zen: spaces, tabs, nested folders, Favorites,
colors and supported icons. Optionally bring over compatible Arc Boosts too.
Preview the changes in your terminal, then apply them with a local backup.
Arc's files are never modified.

## Requirements

- **macOS** for applying a migration. Tested with Zen **1.23.4b on Apple Silicon**.
- An existing Zen profile: open Zen at least once before importing.
- **Python 3.13 or later**. uv can download and manage it for you.
- Zen can stay open while you select features and review the plan. Before writing,
  the wizard can wait for you to quit it or ask Zen to quit after your confirmation.
  Direct `--apply` commands and rollback still require you to quit Zen yourself.

Other platforms can generate offline previews using explicit input paths;
writing to Windows/Linux browser profiles is not supported yet. Writes to other
Zen versions are blocked by default; see [troubleshooting](#troubleshooting).

## Install or run

Choose whichever method you prefer. The package name and command are both
`arc-to-zen`. The PyPI commands below require a published release; if a release
is not available yet, use [the source checkout](#run-from-source).

### Run with uvx

With [uv installed](https://docs.astral.sh/uv/getting-started/installation/),
you can run the tool without a permanent installation:

```shell
uvx arc-to-zen --help
uvx arc-to-zen --wizard
```

`uv tool run arc-to-zen` is equivalent to `uvx arc-to-zen`.

### Install with uv tool

```shell
uv tool install arc-to-zen
arc-to-zen --help
```

To update or remove it later:

```shell
uv tool upgrade arc-to-zen
uv tool uninstall arc-to-zen
```

### Install with pipx

With [pipx](https://pipx.pypa.io/) and Python 3.13 installed:

```shell
pipx install --python python3.13 arc-to-zen
arc-to-zen --help
```

Use `pipx upgrade arc-to-zen` to update or `pipx uninstall arc-to-zen` to remove it.

### Install with pip

Use a virtual environment with Python 3.13 or later. For example, on macOS:

```shell
python3.13 -m venv ~/.venvs/arc-to-zen
source ~/.venvs/arc-to-zen/bin/activate
python -m pip install arc-to-zen
arc-to-zen --help
```

Activate that environment again in a new terminal before running `arc-to-zen`.
Within it, `python -m pip install --upgrade arc-to-zen` updates the tool.

### Run from source

Download or clone this repository and open its directory in a terminal:

```shell
cd arc-to-zen
uv run --no-dev --locked arc-to-zen --help
uv run --no-dev --locked arc-to-zen --wizard
```

uv prepares the environment automatically. `--no-dev` leaves out development
and testing tools. To install the checkout as a persistent command instead:

```shell
uv tool install .
# Or, with Python 3.13 already installed:
pipx install --python python3.13 .
```

The examples below use the installed `arc-to-zen` command. For uvx, prefix each
command with `uvx`; from source, prefix it with `uv run --no-dev --locked`.

## Your first migration

1. Open Zen once so that its profile is initialized.
2. Start the interactive wizard:

   ```sh
   arc-to-zen --wizard
   ```

   If several Zen profiles are available, select one. Choose features by number:

   | Number | Feature |
   | --- | --- |
   | 1 | Pinned tabs and folders |
   | 2 | Unpinned tabs |
   | 3 | Favorites |
   | 4 | Workspace colors and icons |
   | 5 | Site Boosts |

   Enter numbers separated by commas, such as `1,3,4`. The default selects the
   sidebar and appearance; Boosts are optional. Unselected features stay unchanged
   in Zen. Next, select spaces and numbered destinations, Favorites handling,
   duplicate/update policies and whether missing destinations may be created.
   If you choose Boosts, select individual Boosts by number and decide whether to
   skip unsupported ones or import their supported parts. CSS stays unchanged
   unless you explicitly select a static Arc palette.
3. Review the terminal report, including warnings and skipped items.
4. Answer **`yes`** to continue with that plan. If Zen is still running:
   - Quit it yourself and press Enter to check again, or
   - Answer **`Y`** to ask the wizard to quit all Zen windows normally, or
   - Answer **`C`** to cancel without changing the profile.
5. After Zen closes, the wizard rereads its saved state. If the browser saved changes
   after your preview, you see an updated plan and confirm it again. Your feature,
   space and destination choices are kept. New user tabs are preserved.
6. Reopen Zen after the import finishes. The output includes the backup location.

Press Enter or answer `no` at the apply prompt to leave the profile unchanged.
The wizard never force-quits Zen. If normal quitting fails or a dialog remains
open, close it manually or cancel. Another change immediately before writing
also triggers a refresh rather than overwriting the new state.

To use the wizard strictly for preview, without an offer to apply:

```sh
arc-to-zen --wizard --preview-only
```

Outside the wizard, commands only preview unless you add `--apply`. With
`--apply`, they apply immediately after checks, without another confirmation.
`--wizard --apply` still asks for confirmation, but the flag is no longer needed
for interactive imports. `--preview-only` cannot be combined with `--apply`.
Explicit export/report options can create the files you request even in preview
mode. Saved HTML or profile-file previews do not offer an interactive import.

For direct commands, list your spaces and profiles first:

```shell
arc-to-zen --list
arc-to-zen --space Work
arc-to-zen --space Work --space Personal
arc-to-zen --all --duplicates skip
```

For these direct commands, add `--apply` to a reviewed command when ready. If you omit `--space`, `--all`
and `--wizard`, the tool selects **Work**. Quote names containing spaces; use IDs
from `--list` when names are ambiguous.

## What can be transferred?

| Arc content | Result in Zen |
| --- | --- |
| Spaces | Existing or new workspaces, with their names and order |
| Pinned tabs and folders | Sidebar tabs and nested folders, including empty folders, in source order; use `--no-pinned` to omit them |
| Unpinned tabs | Included by default; use `--no-unpinned` to omit them |
| Favorites | Container-specific Essentials; shared once between spaces using the same Arc profile |
| Space appearance | Supported icons, emoji and colors |
| Boosts | Compatible CSS, zaps, fonts, text case and zoom; opt in with `--boosts` |

Arc profiles map to Zen containers. Spaces that share an Arc profile share a
container and its Essentials. Enable container-specific Essentials in Zen if you
use Favorites this way. Alternatively, `--favorites folder` puts Favorites in a
regular folder in each selected workspace; `--favorites skip` omits them.

Colors and gradients are approximate: Zen does not reproduce Arc's materials,
vibrancy, gradient geometry or automatic palettes exactly. Unknown icons retain
the existing/default appearance and are reported. Dynamic folders become ordinary
folders. Image/video backgrounds and folder emoji are not transferred.

Cookies, sign-ins, passwords, browsing history, Arc archives and browser extensions
are not migrated. Expect to sign in to sites again. Split views are unsupported;
by default, an unsupported sidebar item stops the migration. To allow omission,
use `--unsupported skip` and review the skipped-item report.

## Choose the destination and handle existing tabs

By default, matching workspace names are reused and missing workspaces are created.
To choose different names or require an existing destination:

```shell
arc-to-zen --all --map 'Work=Office' --map 'Default=Personal'
arc-to-zen --space Work --zen-space 'Existing workspace name or UUID' --no-create
arc-to-zen --space Work --zen-profile '/path/to/Zen/profile'
```

Use `--zen-profile` to choose a browser profile; `--zen-space` chooses a workspace
inside it. Existing workspaces keep their containers. To select an existing
container for an Arc profile, use `--container 'Arc profile key from --list=2'`.
Conflicting mappings are rejected rather than mixing separate Arc profiles.

Already moved some tabs by hand? Start with `--duplicates skip`. It recognizes
unambiguous matches using the URL **and folder path**, workspace, container and tab
type. The same URL in different folders can remain separate.

| Option | Behavior |
| --- | --- |
| `--mode add` | Default. Add missing items and keep existing records and manual edits. |
| `--mode update` | Reapply imported titles, URLs, structure, order and appearance from Arc. This can overwrite edits to imported items. |
| `--mode skip-existing` | Skip populated destination workspaces, including their appearance. |
| `--duplicates keep` | Default outside the wizard. Keep manually copied tabs separate; report potential matches. |
| `--duplicates skip` | Adopt unambiguous manually copied matches instead of adding duplicates. |

Repeated imports recognize previously imported items. Deleting an item in Arc
does not delete it in Zen. This is a migration tool, not continuous synchronization.
In update mode, a changed source URL replaces that tab's saved history; unchanged
URLs retain their existing history.

To preserve Zen's colors and icons, add `--no-appearance`. To transfer only the
appearance of an existing workspace:

```shell
arc-to-zen --space Work --appearance-only --no-create
```

## Transfer Boosts

Boost migration is optional. Preview Boosts on their own, or include them with tabs:

```shell
arc-to-zen --boosts-only
arc-to-zen --wizard --boosts-only
arc-to-zen --boosts-only --boost-host example.com
arc-to-zen --all --boosts
```

For direct commands, review the plan, quit Zen and add `--apply`. The wizard
handles review, browser closing and confirmation in the same session.

Boosts apply to sites throughout the **selected Zen profile**, across all workspaces
and containers. Selecting Work does not restrict a Boost to Work. Exact hostnames
are supported; URL paths, ports, wildcards and global patterns are skipped.

CSS is copied **as-is**, including `--arc-*` custom properties. Unresolved Arc
variables are noted but do not block the import. If your CSS needs Arc's palette,
you can explicitly add a static light/dark palette from a chosen space:

```shell
arc-to-zen --boosts-only --boost-palette-space Work
```

This adds palette definitions to the CSS. It does not track the active Zen workspace;
gradient backgrounds are approximate. Without this option, no palette is added.

Boosts containing JavaScript, Arc-generated color schemes or unknown settings are
skipped by default. `--allow-partial-boosts` imports only their supported parts;
JavaScript is never installed or executed. The report explains each omission.

Only your own Boosts present in Arc's local cache can be recovered. Incomplete
records are skipped. If the cache changes while reading, close Arc and retry.
You can save the original CSS, JavaScript and settings in a private JSON export,
or reuse an earlier export:

```shell
arc-to-zen --boosts-only --export-boosts /tmp/arc-boosts-new.json
arc-to-zen --boosts-only --boosts-json /tmp/arc-boosts-new.json
```

Repeat `--boost-host` or `--boost-id` to select several Boosts; the two selectors
cannot be combined. Existing active Zen Boosts are kept. An Arc-active Boost is
activated only if its host has no active Boost and there is a single candidate.
Inactive drafts remain inactive, and repeated imports avoid duplicates.

## Preview and export

The normal preview is **text in your terminal**. You do not need to create or open
an HTML file. Optional saved outputs are available:

```shell
arc-to-zen --space Work --report /tmp/arc-zen-report-new.json
arc-to-zen --all --output-dir /tmp/arc-zen-preview-new
arc-to-zen --space Work --preview-html /tmp/work-preview-new.html
```

Choose new file/directory names: existing outputs are never overwritten.
The JSON report records counts, mappings and warnings. `--output-dir` exports the
planned files and report, not a complete usable browser profile. HTML is an optional
offline sidebar outline with change badges and color swatches, not a browser
screenshot. HTML preview cannot be combined with `--apply`.

Saved previews do not apply themselves later. Rerun the command to create a fresh
plan before importing if either browser's data has changed.

## Backups and undo

Every applied change gets a verified local backup under
`arc-to-zen-backups/<timestamp>` inside the selected Zen profile. The command
prints its exact location. Sidebar and Boost changes are backed up together.
Keep that directory if you may want to undo the import.

Preview a rollback, then apply it with Zen closed:

```shell
arc-to-zen --restore '/path/to/profile/arc-to-zen-backups/<timestamp>'
arc-to-zen --restore '/path/to/profile/arc-to-zen-backups/<timestamp>' --apply
```

If you selected a custom profile for the import, pass the same `--zen-profile`
when restoring. Rollback removes unedited additions, preserves tabs you created
later, and reports conflicts where imported content was edited. It also preserves
imported folders that now contain your own tabs, and Boosts/CSS edited after import.
Rollback creates its own backup. Older backups from the first version require
an exact match and cannot perform selective rollback.

Backups and exports contain private browser data. Keep them local and do not share
them as part of a bug report without reviewing their contents.

## Troubleshooting

| Message or problem | What to do |
| --- | --- |
| Zen is running / profile is locked | In the wizard, close it manually and press Enter, choose Y to request normal quitting, or C to cancel. For a direct command, quit Zen with Cmd+Q and retry. |
| Session or containers are missing | Open Zen once, then quit it. Check that `--zen-profile` points to the initialized profile directory. |
| Several Zen profiles are found | Use `--wizard` to select one, or supply `--zen-profile`. |
| Work is missing or a space name is ambiguous | Run `--list`, then select a space explicitly by name or ID. |
| You already moved some tabs manually | Preview with `--duplicates skip`; check that the matches are in the intended folders. |
| An unsupported sidebar item stops the plan | Review the reported item. `--unsupported skip` allows omitting it explicitly. |
| An untested Zen version is blocked | Review a preview first. `--allow-untested-version` permits writing but does not guarantee compatibility. Keep the backup. |
| Arc's Boost cache changes during reading | Close Arc and retry. |
| Your own Boosts cannot be identified | Use the correct Arc account/cache; advanced overrides are `--firestore-cache`, `--arc-auth` and `--boost-owner`. |
| A preview/export file already exists | Choose a new output path. |

For an offline sidebar copy or a nonstandard location, use `--arc-json`:

```shell
arc-to-zen --arc-json '/path/to/StorableSidebar.json' --zen-profile '/path/to/Zen/profile' --all
```

On macOS the usual source is
`~/Library/Application Support/Arc/StorableSidebar.json`; Zen profiles are normally
under `~/Library/Application Support/zen/Profiles/`.
The tool checks for running Zen processes, profile locks and changes to the target
files before writing. It does not visit the URLs being migrated. Network access
may be needed to download the tool or Python; the migration itself reads local data.

Run `arc-to-zen --help` for all options.
Contributor and release instructions are in [CONTRIBUTING.md](CONTRIBUTING.md).
