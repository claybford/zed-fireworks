# zed-fireworks

Sync the [models.dev](https://models.dev) Fireworks AI catalog into Zed's Agent
Panel, so every tool-capable `fireworks-ai` model appears under Zed's
`language_models.openai_compatible.fireworks` provider with the capability set
Zed's schema requires.

One stdlib-only Python file. No API keys are read or written.

## Files

| File | Purpose |
|---|---|
| `zed-fireworks-sync.py` | The whole tool: fetch, merge, write. Also installs the daily systemd user timer. Artifact version is in the module docstring. |
| `fireworks-model-overrides.json` | Hand-maintained per-model patch map, applied last. Ships as `{}`. |

## Requirements

- Python 3.9+ (stdlib only)
- Linux with a systemd user session for `--install-timer`; the sync itself is portable

## Usage

```sh
python3 zed-fireworks-sync.py --dry-run        # print the merged array, write nothing
python3 zed-fireworks-sync.py                 # one refresh
python3 zed-fireworks-sync.py --install-timer # self-copy to ~/.local/lib + daily user timer
python3 zed-fireworks-sync.py --settings PATH # non-default settings.json
```

Default target: `$XDG_CONFIG_HOME/zed/settings.json`, else `~/.config/zed/settings.json`.

## What it writes

Only `language_models.openai_compatible.fireworks` inside `settings.json`:

- `api_url` — asserted to be `https://api.fireworks.ai/inference/v1`
- `available_models` — regenerated from the catalog, sorted by model ID

The file is parsed as JSONC with source offsets, so comments and every other
setting survive byte-for-byte. The generated array is managed: comments *inside
it* are replaced, comments elsewhere are kept. The write is atomic (`tempfile` +
`os.replace`) under a `flock` (a concurrent run fails fast instead of queueing),
and the previous version is copied to `settings.json.fireworks-sync.bak`
(mode 0600) whenever the content actually changes.

Model entries are merged as defaults → existing entry → catalog entry, so
catalog-provided fields are refreshed while non-catalog options you added
survive. Models absent from the latest catalog are retained, so a catalog
omission cannot remove a model you selected or a private one.

## Overrides

Put `{"MODEL_ID": {"field": value}}` in `fireworks-model-overrides.json` **next to
`settings.json`** — the tool reads `settings.parent`, not its own directory, so
on a machine that uses it the file is typically symlinked from the repo into
`~/.config/zed/`.

Overrides apply after the catalog/existing merge, so they win. Use `capabilities`
for nested capability overrides. An override naming a model that is not in the
merged set fails loud on purpose: model renames must be noticed.

## Gotchas (load-bearing)

- **Zed requires the full capability set on every entry.** All seven fields must
  be present; `parallel_tool_calls` and `prompt_cache_key` live *inside*
  `capabilities`, not at the model top level. Omitting one makes Zed reject the
  entire settings file (`missing field parallel_tool_calls`).
- **Reasoning models get `reasoning_effort: "high"` by default.** Zed needs a
  non-null value to expose its generic effort menu. Explicit existing values
  (including `null`) win over the default, and overrides still apply last.
- **The catalog endpoint is asserted** before writing, so models.dev moving or
  renaming the Fireworks provider aborts the sync instead of writing bad
  metadata.
- **Only tool-capable, active models** are emitted: `tool_call: true` and
  `status` not `deprecated`/`retired`.

## Timer

`--install-timer` copies the script to `~/.local/lib/zed-fireworks-sync/`, writes
`zed-fireworks-sync.{service,timer}` into `~/.config/systemd/user/`, then enables
and starts the timer (`OnStartupSec=2min`, `OnUnitActiveSec=1d`). The unit runs
the deployed copy, not the repo checkout, so moving or renaming this repo does
not disturb it — re-run `--install-timer` after changing the script.

```sh
systemctl --user status zed-fireworks-sync.timer
journalctl --user -u zed-fireworks-sync.service
systemctl --user disable --now zed-fireworks-sync.timer
```

## Not in this repo

Zed's own config (`settings.json`, `keymap.json`, `themes/`) is app state and is
deliberately untracked; this tool manages exactly one array inside it.
