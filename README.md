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
pyxyproj config init [--global]   # write a starter pyxyproj.yaml
pyxyproj config show | path
```

`new` flags: `-p` preset, `-l` language, `-d` parent folder, `-o KEY=VALUE`, `--port`,
`--author`, `--license`, `--description`, `--git/--no-git`, `--install/--no-install`,
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

`↑/↓` or `j/k` move, `←/→` or `h/l` change a choice, `Enter` edits text / toggles,
`g` generates, `q` or `Esc` quits. A live file-tree preview updates as you change fields.

## Adding a built-in preset

Write a `build_xxx(ctx) -> (dirs, files)` function and add a `Preset(...)` to
`builtin_presets()`. That is all the CLI, TUI and config need.
