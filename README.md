# PyxyProj

A project scaffolder with a **CLI** and a **TUI**, driven by a **YAML config**.
One file (`pyxyproj.py`), standard library only, Python 3.9+. PyYAML is used if it is
installed; otherwise a built-in YAML reader handles the config.

## Install

```sh
chmod +x pyxyproj.py
ln -s "$PWD/pyxyproj.py" ~/.local/bin/pyxyproj     # or just: python3 pyxyproj.py ...
```

## Use

```sh
pyxyproj                          # open the TUI
pyxyproj new my-app               # Node.js + Express (default preset)
pyxyproj new my-app -l ts         # TypeScript
pyxyproj new my-app -o framework=http   # zero-dependency Node (no Express)
pyxyproj new tool -l python       # language picks a matching preset
pyxyproj new site -p static
pyxyproj new my-app -n            # dry run: show the file tree only
pyxyproj presets [-l go]          # list presets / filter by language
pyxyproj new my-app -L work       # create it in a saved location
pyxyproj locations                # list saved locations
pyxyproj locations add work ~/code
pyxyproj locations remove work
pyxyproj history                  # everything pyxyproj has generated
pyxyproj config init [--global]   # write a starter pyxyproj.yaml
pyxyproj config show | path
```

`new` flags: `-p` preset, `-l` language, `-d` parent folder, `-o KEY=VALUE`, `--port`,
`--author`, `--license`, `--description`, `--git/--no-git`, `--install/--no-install`,
`-L NAME` saved location (instead of `-d`), `--no-history`,
`-f` write into a non-empty folder, `-n` dry run, `-q` quiet.
`pyxyproj new .` scaffolds into the current folder.

## Built-in presets

| preset | languages | creates |
|---|---|---|
| `node` | `js`, `ts` | `public/index.html` (**empty**), `server`, `routes`, `package.json` with `start` + `dev` scripts. Option `framework=express\|http` |
| `static` | `html` | `public/` with index.html, style.css, main.js |
| `python` | `py` | package with CLI entry point, `pyproject.toml`, unittest tests |
| `c` | `c` | `Makefile`, `src/main.c` |
| `go` | `go` | `go.mod`, `main.go` |
| `rust` | `rust` | `Cargo.toml`, `src/main.rs` |

Every project also gets `README.md`, `.gitignore`, and a `LICENSE` (MIT) unless switched off.

Node scripts: JS uses `node server.js` / `node --watch server.js`. TS uses `tsx watch` for
`dev`, and `start` builds with `tsc` then runs `dist/`.

## Locations

A list of folders you create projects in often. Add them three ways:

```yaml
# pyxyproj.yaml (mapping name: path, or just a list of paths)
locations:
  work: ~/code
  play: ~/sandbox
defaults:
  location: work        # optional: use this one unless -d / -L is given
```

```sh
pyxyproj locations add work ~/code     # saved in ~/.config/pyxyproj/locations.yaml
pyxyproj new my-app -L work
```

In the TUI, `←/→` on **Location** cycles through `here` plus every saved location.
`a` saves the current path under a name, `x` forgets a saved one (config-defined
locations are read-only there). Resolution order: `-d` > `-L` > `defaults.location` > `defaults.output_dir`.

## History

Every project pyxyproj creates is recorded in `~/.local/share/pyxyproj/history.json`
(name, path, preset, language, options, port, time). Dry runs are not recorded.

```sh
pyxyproj history                 # newest first; folders that were deleted are marked (missing)
pyxyproj history -n 5 --json
pyxyproj history --missing       # only entries whose folder is gone
pyxyproj history --prune         # forget those entries
pyxyproj history --clear         # forget everything (never deletes project folders)
```

In the TUI press `H` for the same list. `Enter` copies that project's preset, language,
options, port and parent folder into the form, which is handy for "another one like that".
`x` forgets an entry. Turn it off with `history.enabled: false`, or per run with `--no-history`.

## Tab completion (TUI)

While editing a path (Location): `Tab` completes folders (common prefix first, `~` works),
`Tab` again cycles through the candidates, `Ctrl-W` removes the last path component,
`Ctrl-U` clears. In your normal shell, paths such as `-d` are already completed by the shell.

## Config

Read in order, later wins: `~/.config/pyxyproj/config.yaml` then `./pyxyproj.yaml`
(or only `--config FILE`). See `pyxyproj.example.yaml` for every key. Highlights:

```yaml
defaults:    { preset: node, port: 3000, git: true, install: false, output_dir: ., author: "" }
paths:       { public_dir: public, src_dir: ., ts_src_dir: src, out_dir: dist,
               server_file: server, routes_file: routes }
options:     { node: { framework: express } }
files:       { readme: true, gitignore: true, license: true, editorconfig: false }
hooks:       { post: ["code ."] }
```

All paths are relative to the new project and may not escape it.

### Custom presets

```yaml
presets:
  my-api:
    description: Express API with tests
    extends: node            # start from a built-in preset (optional)
    dirs: [tests, docs]
    files:
      tests/health.test.js: |
        // tests for {{name}}
      .env.example: |
        PORT={{port}}
      LICENSE: null          # null removes a file the base preset created
    install: npm install
    next: [npm install, npm run dev]
```

Template variables: `{{name}} {{slug}} {{pkg}} {{lang}} {{port}} {{author}} {{license}}
{{year}} {{description}} {{public_dir}} {{src_dir}} {{out_dir}} {{server_file}} {{routes_file}}`.

## TUI keys

`↑/↓` or `j/k` move, `←/→` or `h/l` change a choice or cycle saved locations,
`Enter` edits text / toggles, `g` generates, `H` opens history, `q` or `Esc` quits.
On Location: `a` saves it, `x` forgets it. A live file-tree preview updates as you change fields.
The terminal needs to be at least 44x20.

## Adding a built-in preset

Write a `build_xxx(ctx) -> (dirs, files)` function and add a `Preset(...)` to
`builtin_presets()`. That is all the CLI, TUI and config need.
