#!/usr/bin/env python3
"""PyxyProj - scaffold projects from a CLI or a TUI, driven by a YAML config.

Single file, standard library only (PyYAML is used when installed, otherwise a
built-in YAML-subset reader takes over). Python 3.9+.

    pyxyproj                         open the TUI
    pyxyproj new my-app              Node.js project (default preset)
    pyxyproj new my-app -l ts        TypeScript variant
    pyxyproj new tool -p python      another preset
    pyxyproj new my-app -L work      create it in a saved location
    pyxyproj locations add work ~/code
    pyxyproj history                 projects generated so far
    pyxyproj presets                 list presets and languages
    pyxyproj config init             write a starter pyxyproj.yaml
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import locale
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

__version__ = "0.1.0"
APP = "pyxyproj"


class PyxyError(Exception):
    """A user-facing error (printed without a traceback)."""


# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #

def _color_ok() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _color_ok() else text


def _utf() -> bool:
    return "utf" in (getattr(sys.stdout, "encoding", None) or "").lower()


def glyphs() -> dict:
    if _utf():
        return {"t": "├── ", "l": "└── ", "bar": "│   ", "sp": "    ", "ok": "✔", "dot": "·", "arrow": "→"}
    return {"t": "|-- ", "l": "`-- ", "bar": "|   ", "sp": "    ", "ok": "+", "dot": "-", "arrow": "->"}


# --------------------------------------------------------------------------- #
# Minimal YAML (subset) - used only when PyYAML is not installed
#   mappings, lists, scalars, inline [a, b] / {a: b}, quoted strings,
#   comments, and | / > block scalars. Enough for pyxyproj configs.
# --------------------------------------------------------------------------- #

def _strip_comment(s: str) -> str:
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and (i == 0 or s[i - 1] in " \t[{,:"):
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s


def _split_inline(s: str) -> list:
    parts, depth, quote, cur = [], 0, None, ""
    for ch in s:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch in "[{":
            depth += 1
            cur += ch
        elif ch in "]}":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _scalar(s: str):
    s = s.strip()
    if s == "" or s in ("~", "null", "Null", "NULL"):
        return None
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        inner = s[1:-1]
        if s[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
        return inner
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [] if not inner else [_scalar(p) for p in _split_inline(inner)]
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        out = {}
        for part in _split_inline(inner) if inner else []:
            k, _, v = part.partition(":")
            out[str(_scalar(k))] = _scalar(v)
        return out
    return s


class _YamlReader:
    KEY = re.compile(r"^(\"[^\"]*\"|'[^']*'|[^:#]+?)\s*:(\s+(.*))?$")

    def __init__(self, text: str):
        self.lines = text.replace("\t", "  ").splitlines()
        self.i = 0

    def cur(self):
        while self.i < len(self.lines):
            t = self.lines[self.i].strip()
            if t == "" or t.startswith("#"):
                self.i += 1
                continue
            line = self.lines[self.i]
            return len(line) - len(line.lstrip(" ")), t
        return None

    def parse(self, indent: int):
        c = self.cur()
        if c is None or c[0] < indent:
            return None
        ind, t = c
        if t == "-" or t.startswith("- "):
            return self.parse_list(ind)
        return self.parse_map(ind)

    def parse_list(self, ind: int):
        out = []
        while True:
            c = self.cur()
            if c is None or c[0] != ind or not (c[1] == "-" or c[1].startswith("- ")):
                break
            rest = c[1][1:].strip()
            if rest == "":
                self.i += 1
                out.append(self.parse(ind + 1))
            elif re.match(r"^[^\s\"'\[{][^:]*:(\s|$)", rest):
                self.lines[self.i] = " " * (ind + 2) + rest
                out.append(self.parse_map(ind + 2))
            else:
                self.i += 1
                out.append(_scalar(_strip_comment(rest)))
        return out

    def parse_map(self, ind: int):
        out = {}
        while True:
            c = self.cur()
            if c is None or c[0] != ind:
                break
            m = self.KEY.match(c[1])
            if not m:
                raise ValueError(f"line {self.i + 1}: cannot parse {c[1]!r}")
            key = str(_scalar(m.group(1)))
            val = _strip_comment(m.group(3) or "").strip()
            self.i += 1
            if val in ("|", "|-", "|+", ">", ">-"):
                out[key] = self.block(ind, val)
            elif val == "":
                nxt = self.cur()
                if nxt and nxt[0] > ind:
                    out[key] = self.parse(nxt[0])
                elif nxt and nxt[0] == ind and (nxt[1] == "-" or nxt[1].startswith("- ")):
                    out[key] = self.parse_list(ind)
                else:
                    out[key] = None
            else:
                out[key] = _scalar(val)
        return out

    def block(self, ind: int, style: str) -> str:
        lines, bi = [], None
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if raw.strip() == "":
                lines.append("")
                self.i += 1
                continue
            li = len(raw) - len(raw.lstrip(" "))
            if li <= ind or (bi is not None and li < bi):
                break
            if bi is None:
                bi = li
            lines.append(raw[bi:])
            self.i += 1
        while lines and lines[-1] == "":
            lines.pop()
        text = "\n".join(lines)
        if style.startswith(">"):
            text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
        return text if style.endswith("-") else text + "\n"


def load_yaml(text: str):
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    try:
        if yaml is not None:
            return yaml.safe_load(text) or {}
        reader = _YamlReader(text)
        data = reader.parse(0)
        if reader.cur() is not None:
            raise ValueError(f"line {reader.i + 1}: unexpected indentation")
        return data or {}
    except Exception as exc:  # noqa: BLE001 - surface parser errors uniformly
        raise PyxyError(f"invalid YAML: {exc}") from exc


def _fmt_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if (s == "" or s != s.strip() or s.lower() in ("true", "false", "yes", "no", "null", "~")
            or re.fullmatch(r"-?\d+(\.\d+)?", s) or s[0] in "[{&*!|>'\"%@`#-?:,"
            or ": " in s or " #" in s):
        return json.dumps(s, ensure_ascii=False)
    return s


def dump_yaml(data, indent: int = 0) -> str:
    pad = "  " * indent
    out: list = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and v:
                out += [f"{pad}{k}:", dump_yaml(v, indent + 1)]
            elif isinstance(v, list) and v:
                out += [f"{pad}{k}:", dump_yaml(v, indent + 1)]
            elif isinstance(v, (dict, list)):
                out.append(f"{pad}{k}: {{}}" if isinstance(v, dict) else f"{pad}{k}: []")
            elif isinstance(v, str) and "\n" in v:
                out.append(f"{pad}{k}: |" if v.endswith("\n") else f"{pad}{k}: |-")
                out += [f"{pad}  {ln}" if ln else "" for ln in v.rstrip("\n").split("\n")]
            else:
                out.append(f"{pad}{k}: {_fmt_scalar(v)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)) and item:
                out += [f"{pad}-", dump_yaml(item, indent + 1)]
            else:
                out.append(f"{pad}- {_fmt_scalar(item)}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict = {
    "defaults": {
        "preset": "node",
        "language": None,
        "output_dir": ".",
        "location": None,
        "author": "",
        "license": "MIT",
        "description": "",
        "port": 3000,
        "git": True,
        "install": False,
    },
    "paths": {
        "public_dir": "public",
        "src_dir": ".",
        "ts_src_dir": "src",
        "out_dir": "dist",
        "server_file": "server",
        "routes_file": "routes",
    },
    "options": {"node": {"framework": "express"}},
    "files": {"readme": True, "gitignore": True, "license": True, "editorconfig": False},
    "hooks": {"post": []},
    "locations": {},
    "history": {"enabled": True, "limit": 500, "file": None},
    "presets": {},
}

CONFIG_TEMPLATE = """\
# PyxyProj configuration
# Read in this order (later wins):
#   ~/.config/pyxyproj/config.yaml  ->  ./pyxyproj.yaml  (or --config FILE only)

defaults:
  preset: node          # see `pyxyproj presets`
  language: null        # null = first language of the preset (js, ts, py, c, go, rust, html)
  output_dir: .         # folder new projects are created in
  location: null        # or: the name of a saved location (see `locations` below)
  author: ""
  license: MIT          # MIT also writes a LICENSE file; other values are only recorded in manifests
  description: ""
  port: 3000
  git: true             # run `git init`
  install: false        # run the preset's install step (npm install, go mod tidy, ...)

paths:                  # all relative to the new project root
  public_dir: public    # static files; index.html is created here (empty for the node preset)
  src_dir: .            # node/js: where server.js and routes.js live
  ts_src_dir: src       # node/ts: where server.ts and routes.ts live
  out_dir: dist         # node/ts: compiled output
  server_file: server   # base file names, the extension follows the language
  routes_file: routes

options:                # per-preset option defaults
  node:
    framework: express  # express | http  (http = zero dependencies)

files:                  # extras every project gets
  readme: true
  gitignore: true
  license: true
  editorconfig: false

hooks:
  post: []              # shell commands run inside the new project; {{name}} {{slug}} {{lang}} work
  # post:
  #   - code .

# Selectable default locations: left/right on "Location" in the TUI, `new -L NAME` on the CLI.
# Also manageable with `pyxyproj locations add NAME PATH` (those are kept in a separate file).
locations:
  # work: ~/code
  # play: ~/sandbox
  # clients: /srv/clients

history:                # `pyxyproj history`, and H in the TUI
  enabled: true
  limit: 500            # newest entries kept; 0 = unlimited
  file: null            # null = ~/.local/share/pyxyproj/history.json

# Custom presets. `extends` starts from a built-in preset; `files` entries are
# rendered with {{name}} {{slug}} {{pkg}} {{port}} {{author}} {{year}} {{lang}}
# {{public_dir}} ... and a `null` value removes a file the base preset made.
#
# presets:
#   my-api:
#     description: Express API with tests
#     extends: node
#     dirs: [tests, docs]
#     files:
#       tests/health.test.js: |
#         // tests for {{name}}
#       .env.example: |
#         PORT={{port}}
#       public/index.html: null
#     install: npm install
#     next: [npm install, npm run dev]
"""


def deep_merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = deep_merge(base[k], v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def user_config_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(root) / APP / "config.yaml"


def local_config_path() -> Optional[Path]:
    for name in ("pyxyproj.yaml", "pyxyproj.yml", ".pyxyproj.yaml"):
        p = Path.cwd() / name
        if p.is_file():
            return p
    return None


def safe_rel(p, what: str = "path") -> str:
    s = posixpath.normpath(str(p).replace("\\", "/"))
    if s.startswith("/") or s == ".." or s.startswith("../") or re.match(r"^[A-Za-z]:", s):
        raise PyxyError(f"{what} must stay inside the project (got {p!r})")
    return s


def load_config(explicit: Optional[str] = None):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    sources: list = []
    if explicit:
        files = [Path(explicit).expanduser()]
        if not files[0].is_file():
            raise PyxyError(f"config file not found: {files[0]}")
    else:
        files = [p for p in (user_config_path(), local_config_path()) if p and p.is_file()]
    for f in files:
        try:
            data = load_yaml(f.read_text(encoding="utf-8"))
        except PyxyError as exc:
            raise PyxyError(f"{f}: {exc}") from exc
        if not isinstance(data, dict):
            raise PyxyError(f"{f}: top level must be a mapping")
        cfg = deep_merge(cfg, data)
        sources.append(f)
    for section in ("defaults", "paths", "options", "files", "hooks", "history", "presets"):
        if not isinstance(cfg.get(section), dict):
            if cfg.get(section) is None:
                cfg[section] = {}
            else:
                raise PyxyError(f"config section '{section}' must be a mapping")
    if cfg.get("locations") is None:
        cfg["locations"] = {}
    if not isinstance(cfg["locations"], (dict, list)):
        raise PyxyError("config section 'locations' must be a mapping (name: path) or a list of paths")
    for k in ("public_dir", "src_dir", "ts_src_dir", "out_dir"):
        cfg["paths"][k] = safe_rel(cfg["paths"][k], f"paths.{k}")
    for k in ("server_file", "routes_file"):
        if not re.fullmatch(r"[\w.-]+", str(cfg["paths"][k])):
            raise PyxyError(f"paths.{k} must be a plain file name (got {cfg['paths'][k]!r})")
    if not isinstance(cfg["hooks"].get("post") or [], list):
        raise PyxyError("hooks.post must be a list of commands")
    return cfg, sources


# --------------------------------------------------------------------------- #
# Saved locations and project history
# --------------------------------------------------------------------------- #

def short_path(p) -> str:
    p = str(p)
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def fit(text: str, n: int) -> str:
    """Trim from the left so the interesting end of a path stays visible."""
    if n <= 1 or len(text) <= n:
        return text
    return ("…" if _utf() else "~") + text[-(n - 1):]


def same_path(a, b) -> bool:
    def norm(p):
        return os.path.normpath(os.path.abspath(os.path.expanduser(str(p))))
    return norm(a) == norm(b)


def data_dir() -> Path:
    root = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(root) / APP


def locations_file() -> Path:
    return user_config_path().parent / "locations.yaml"


LOC_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _norm_locations(raw, source: str) -> list:
    out = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v not in (None, ""):
                out.append((str(k), str(v), source))
    elif isinstance(raw, list):
        for v in raw:
            p = str(v)
            name = Path(os.path.expanduser(p)).name or p
            out.append((name, p, source))
    elif raw:
        raise PyxyError("'locations' must be a mapping (name: path) or a list of paths")
    return out


def load_saved_locations() -> dict:
    f = locations_file()
    if not f.is_file():
        return {}
    try:
        data = load_yaml(f.read_text(encoding="utf-8"))
    except PyxyError as exc:
        raise PyxyError(f"{f}: {exc}") from exc
    if not isinstance(data, dict):
        raise PyxyError(f"{f}: expected 'name: path' pairs")
    return {str(k): str(v) for k, v in data.items() if v}


def write_saved_locations(data: dict) -> None:
    f = locations_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    head = "# Managed by pyxyproj (`pyxyproj locations`, TUI). Hand-written ones can live in your config.\n"
    f.write_text(head + (dump_yaml(data) + "\n" if data else ""), encoding="utf-8")


def all_locations(cfg: dict) -> list:
    """[(name, path, source)] - config entries first, then saved ones."""
    seen, out = set(), []
    for name, path, src in _norm_locations(cfg.get("locations"), "config") + \
            _norm_locations(load_saved_locations(), "saved"):
        if name not in seen:
            seen.add(name)
            out.append((name, path, src))
    return out


def resolve_location(cfg: dict, name: str) -> str:
    if name == "here":
        return "."
    for n, p, _ in all_locations(cfg):
        if n == name:
            return p
    known = ", ".join(["here"] + [n for n, _, _ in all_locations(cfg)])
    raise PyxyError(f"unknown location '{name}' (available: {known})")


def default_parent(cfg: dict) -> str:
    d = cfg["defaults"]
    if d.get("location"):
        return resolve_location(cfg, str(d["location"]))
    return str(d.get("output_dir", "."))


def add_saved_location(cfg: dict, name: str, path) -> str:
    name = (name or "").strip()
    if not LOC_NAME.match(name):
        raise PyxyError("location names may only use letters, digits, '.', '_' and '-'")
    if name == "here":
        raise PyxyError("'here' is reserved (it means the current folder)")
    if any(n == name and s == "config" for n, _, s in all_locations(cfg)):
        raise PyxyError(f"'{name}' is defined in your config file; choose another name")
    resolved = Path(os.path.expanduser(str(path or "."))).resolve()
    saved = load_saved_locations()
    saved[name] = short_path(resolved)
    write_saved_locations(saved)
    return saved[name]


def remove_saved_location(cfg: dict, name: str) -> None:
    saved = load_saved_locations()
    if name in saved:
        del saved[name]
        write_saved_locations(saved)
        return
    if any(n == name for n, _, _ in all_locations(cfg)):
        raise PyxyError(f"'{name}' comes from your config file - remove it there")
    raise PyxyError(f"unknown location '{name}'")


def history_path(cfg: dict) -> Path:
    custom = (cfg.get("history") or {}).get("file")
    return Path(os.path.expanduser(str(custom))) if custom else data_dir() / "history.json"


def load_history(cfg: dict) -> list:
    """Newest first. A corrupt file is moved aside instead of being overwritten."""
    f = history_path(cfg)
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        entries = data.get("projects", []) if isinstance(data, dict) else []
        return [e for e in entries if isinstance(e, dict) and e.get("path")]
    except (OSError, ValueError):
        try:
            f.replace(f.with_suffix(".json.bak"))
        except OSError:
            pass
        return []


def save_history(cfg: dict, entries: list) -> None:
    f = history_path(cfg)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": 1, "projects": entries}, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, f)


def record_history(cfg: dict, plan: "Plan") -> None:
    conf = cfg.get("history") or {}
    if not conf.get("enabled", True):
        return
    try:
        path = str(plan.root)
        entries = [e for e in load_history(cfg) if e.get("path") != path]
        entries.insert(0, {
            "name": plan.name, "path": path, "preset": plan.preset.key, "lang": plan.lang,
            "opts": plan.opts, "port": plan.ctx.get("port"),
            "created": datetime.datetime.now().isoformat(timespec="seconds"), "version": __version__,
        })
        limit = int(conf.get("limit") or 0)
        save_history(cfg, entries[:limit] if limit > 0 else entries)
    except (OSError, ValueError):
        pass  # history is a convenience; never fail a generation because of it


def forget_history(cfg: dict, path: str) -> None:
    save_history(cfg, [e for e in load_history(cfg) if e.get("path") != path])


# --------------------------------------------------------------------------- #
# Languages, naming, rendering
# --------------------------------------------------------------------------- #

LANGS = {"js": "JavaScript", "ts": "TypeScript", "py": "Python", "c": "C",
         "go": "Go", "rust": "Rust", "html": "HTML/CSS/JS", "generic": "Generic"}
LANG_ALIASES = {"javascript": "js", "node": "js", "nodejs": "js", "typescript": "ts",
                "python": "py", "python3": "py", "golang": "go", "rs": "rust", "web": "html"}


def norm_lang(lang):
    if lang is None:
        return None
    lang = str(lang).strip().lower()
    return LANG_ALIASES.get(lang, lang)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-._")
    return s or "project"


def pkgname(slug: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", slug)
    return "p_" + s if not s or s[0].isdigit() else s


def pj(*parts: str) -> str:
    return posixpath.normpath(posixpath.join(*parts))


_VAR = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")


def render(text: str, ctx: dict) -> str:
    return _VAR.sub(lambda m: str(ctx.get(m.group(1), m.group(0))), text)


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

MIME_JS = """\
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
};
"""

NODE_JS_EXPRESS = {
    "server": """\
const path = require('path');
const express = require('express');
const routes = require('{{routes_mod}}');

const app = express();
const PORT = process.env.PORT || {{port}};
const PUBLIC_DIR = path.join(__dirname, '{{public_rel}}');

app.use(express.json());
app.use(express.static(PUBLIC_DIR));
app.use('/', routes);

app.listen(PORT, () => {
  console.log(`{{slug}} running at http://localhost:${PORT}`);
});
""",
    "routes": """\
const express = require('express');

const router = express.Router();

router.get('/api/health', (req, res) => {
  res.json({ ok: true, uptime: process.uptime() });
});

module.exports = router;
""",
}

NODE_JS_HTTP = {
    "server": """\
const http = require('http');
const fs = require('fs');
const path = require('path');
const routes = require('{{routes_mod}}');

const PORT = process.env.PORT || {{port}};
const PUBLIC_DIR = path.resolve(__dirname, '{{public_rel}}');

""" + MIME_JS + """
function serveStatic(req, res) {
  const { pathname } = new URL(req.url, 'http://localhost');
  let rel = decodeURIComponent(pathname);
  if (rel.endsWith('/')) rel += 'index.html';

  const file = path.normalize(path.join(PUBLIC_DIR, rel));
  if (!file.startsWith(PUBLIC_DIR + path.sep)) {
    res.writeHead(403, { 'Content-Type': 'text/plain' });
    return res.end('Forbidden');
  }

  fs.readFile(file, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      return res.end('Not found');
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(data);
  });
}

const server = http.createServer(async (req, res) => {
  try {
    if (await routes.handle(req, res)) return;
    serveStatic(req, res);
  } catch (err) {
    console.error(err);
    res.writeHead(500, { 'Content-Type': 'text/plain' });
    res.end('Internal Server Error');
  }
});

server.listen(PORT, () => {
  console.log(`{{slug}} running at http://localhost:${PORT}`);
});
""",
    "routes": """\
function json(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(body));
}

// 'METHOD /path' -> handler(req, res)
const routes = {
  'GET /api/health': (req, res) => json(res, 200, { ok: true, uptime: process.uptime() }),
};

// Returns true when a route handled the request.
async function handle(req, res) {
  const { pathname } = new URL(req.url, 'http://localhost');
  const handler = routes[`${req.method} ${pathname}`];
  if (!handler) return false;
  await handler(req, res);
  return true;
}

module.exports = { routes, handle, json };
""",
}

NODE_TS_EXPRESS = {
    "server": """\
import path from 'node:path';
import express from 'express';
import routes from '{{routes_mod}}';

const app = express();
const PORT = Number(process.env.PORT) || {{port}};
// Resolved from the project root so it works for both `dev` (tsx) and `start` (compiled).
const PUBLIC_DIR = path.resolve(process.cwd(), '{{public_dir}}');

app.use(express.json());
app.use(express.static(PUBLIC_DIR));
app.use('/', routes);

app.listen(PORT, () => {
  console.log(`{{slug}} running at http://localhost:${PORT}`);
});
""",
    "routes": """\
import { Router } from 'express';

const router = Router();

router.get('/api/health', (_req, res) => {
  res.json({ ok: true, uptime: process.uptime() });
});

export default router;
""",
}

NODE_TS_HTTP = {
    "server": """\
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { handle } from '{{routes_mod}}';

const PORT = Number(process.env.PORT) || {{port}};
// Resolved from the project root so it works for both `dev` (tsx) and `start` (compiled).
const PUBLIC_DIR = path.resolve(process.cwd(), '{{public_dir}}');

""" + MIME_JS.replace("const MIME = {", "const MIME: Record<string, string> = {") + """
function serveStatic(req: http.IncomingMessage, res: http.ServerResponse): void {
  const { pathname } = new URL(req.url ?? '/', 'http://localhost');
  let rel = decodeURIComponent(pathname);
  if (rel.endsWith('/')) rel += 'index.html';

  const file = path.normalize(path.join(PUBLIC_DIR, rel));
  if (!file.startsWith(PUBLIC_DIR + path.sep)) {
    res.writeHead(403, { 'Content-Type': 'text/plain' });
    res.end('Forbidden');
    return;
  }

  fs.readFile(file, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Not found');
      return;
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] ?? 'application/octet-stream' });
    res.end(data);
  });
}

const server = http.createServer(async (req, res) => {
  try {
    if (await handle(req, res)) return;
    serveStatic(req, res);
  } catch (err) {
    console.error(err);
    res.writeHead(500, { 'Content-Type': 'text/plain' });
    res.end('Internal Server Error');
  }
});

server.listen(PORT, () => {
  console.log(`{{slug}} running at http://localhost:${PORT}`);
});
""",
    "routes": """\
import type { IncomingMessage, ServerResponse } from 'node:http';

type Handler = (req: IncomingMessage, res: ServerResponse) => void | Promise<void>;

export function json(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(body));
}

// 'METHOD /path' -> handler(req, res)
export const routes: Record<string, Handler> = {
  'GET /api/health': (_req, res) => json(res, 200, { ok: true, uptime: process.uptime() }),
};

// Returns true when a route handled the request.
export async function handle(req: IncomingMessage, res: ServerResponse): Promise<boolean> {
  const { pathname } = new URL(req.url ?? '/', 'http://localhost');
  const handler = routes[`${req.method} ${pathname}`];
  if (!handler) return false;
  await handler(req, res);
  return true;
}
""",
}

NODE_TEMPLATES = {
    ("js", "express"): NODE_JS_EXPRESS, ("js", "http"): NODE_JS_HTTP,
    ("ts", "express"): NODE_TS_EXPRESS, ("ts", "http"): NODE_TS_HTTP,
}

GITIGNORE = {
    "js": "node_modules/\n.env\n.env.*\n!.env.example\n*.log\n.DS_Store\n",
    "ts": "node_modules/\ndist/\n.env\n.env.*\n!.env.example\n*.log\n*.tsbuildinfo\n.DS_Store\n",
    "py": "__pycache__/\n*.py[cod]\n*.egg-info/\n.venv/\nvenv/\nbuild/\ndist/\n.pytest_cache/\n.env\n.DS_Store\n",
    "c": "build/\n{{slug}}\n*.o\n*.exe\n.DS_Store\n",
    "go": "{{slug}}\n*.exe\nbin/\n.env\n.DS_Store\n",
    "rust": "target/\n.env\n.DS_Store\n",
    "html": ".DS_Store\n",
    "generic": ".env\n.DS_Store\n",
}

EDITORCONFIG = """\
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
indent_style = space
indent_size = 2
trim_trailing_whitespace = true

[*.{py,rs}]
indent_size = 4

[{Makefile,*.go}]
indent_style = tab
"""

MIT = """\
MIT License

Copyright (c) {{year}} {{author}}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #

@dataclass
class Preset:
    key: str
    description: str
    languages: list
    build: Callable[[dict], tuple]
    install: Optional[list] = None
    options: dict = field(default_factory=dict)       # option -> [choices]
    next_steps: list = field(default_factory=list)
    takes_port: bool = False
    base: str = ""                                    # built-in key (for option defaults)
    overlay: Optional[Callable] = None                # custom presets: edit (ctx, dirs, files)
    custom: bool = False


def build_node(ctx: dict) -> tuple:
    lang = ctx["lang"]
    ts = lang == "ts"
    fw = ctx["opts"].get("framework", "express")
    ext = "ts" if ts else "js"
    src = ctx["ts_src_dir"] if ts else ctx["src_dir"]
    server = pj(src, f"{ctx['server_file']}.{ext}")
    routes = pj(src, f"{ctx['routes_file']}.{ext}")
    c = dict(ctx, server_path=server,
             routes_mod="./" + ctx["routes_file"],
             public_rel=posixpath.relpath(ctx["public_dir"], src))
    tpl = NODE_TEMPLATES[(lang, fw)]

    pkg: dict = {
        "name": ctx["slug"],
        "version": "0.1.0",
        "description": ctx["description"],
        "private": True,
        "main": server if not ts else pj(ctx["out_dir"], f"{ctx['server_file']}.js"),
        "scripts": {},
        "engines": {"node": ">=18.11"},
    }
    if ctx["author"]:
        pkg["author"] = ctx["author"]
    if ctx["license"]:
        pkg["license"] = ctx["license"]

    files = {
        pj(ctx["public_dir"], "index.html"): "",
        server: render(tpl["server"], c),
        routes: render(tpl["routes"], c),
    }

    if ts:
        out_server = pj(ctx["out_dir"], f"{ctx['server_file']}.js")
        pkg["scripts"] = {
            "build": "tsc",
            "prestart": "npm run build",
            "start": f"node {out_server}",
            "dev": f"tsx watch {server}",
            "typecheck": "tsc --noEmit",
        }
        pkg["engines"] = {"node": ">=18.17"}
        dev = {"@types/node": "^22.10.0", "tsx": "^4.19.0", "typescript": "^5.6.0"}
        if fw == "express":
            dev["@types/express"] = "^5.0.0"
        pkg["devDependencies"] = dict(sorted(dev.items()))
        files["tsconfig.json"] = json.dumps({
            "compilerOptions": {
                "target": "ES2022", "module": "NodeNext", "outDir": ctx["out_dir"], "rootDir": src,
                "strict": True, "esModuleInterop": True, "skipLibCheck": True,
                "forceConsistentCasingInFileNames": True,
            },
            "include": [pj(src, "**/*")],
            "exclude": ["node_modules", ctx["out_dir"]],
        }, indent=2) + "\n"
    else:
        pkg["scripts"] = {"start": f"node {server}", "dev": f"node --watch {server}"}

    if fw == "express":
        pkg["dependencies"] = {"express": ctx.get("express_version", "^5.1.0")}
    files["package.json"] = json.dumps(pkg, indent=2) + "\n"
    return [], files


def build_static(ctx: dict) -> tuple:
    pub = ctx["public_dir"]
    return [], {
        pj(pub, "index.html"): render("""\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{name}}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main>
    <h1>{{name}}</h1>
    <p>{{description}}</p>
  </main>
  <script src="main.js"></script>
</body>
</html>
""", ctx),
        pj(pub, "style.css"): ":root {\n  color-scheme: light dark;\n}\n\nbody {\n  margin: 0;\n  font-family: system-ui, sans-serif;\n}\n\nmain {\n  max-width: 40rem;\n  margin: 4rem auto;\n  padding: 0 1rem;\n}\n",
        pj(pub, "main.js"): "console.log('hello from {{slug}}');\n".replace("{{slug}}", ctx["slug"]),
    }


def build_python(ctx: dict) -> tuple:
    pkg = ctx["pkg"]
    lines = [
        "[build-system]", 'requires = ["setuptools>=68"]', 'build-backend = "setuptools.build_meta"', "",
        "[project]", f'name = "{ctx["slug"]}"', 'version = "0.1.0"',
        f"description = {json.dumps(ctx['description'])}", 'readme = "README.md"',
        'requires-python = ">=3.9"', "dependencies = []",
    ]
    if ctx["license"]:
        lines.append(f'license = {{text = "{ctx["license"]}"}}')
    if ctx["author"]:
        lines.append(f"authors = [{{name = {json.dumps(ctx['author'])}}}]")
    lines += ["", "[project.scripts]", f'{ctx["slug"]} = "{pkg}.cli:main"', "",
              "[tool.setuptools.packages.find]", f'include = ["{pkg}*"]', ""]
    return ["tests"], {
        "pyproject.toml": "\n".join(lines),
        f"{pkg}/__init__.py": '__version__ = "0.1.0"\n',
        f"{pkg}/__main__.py": f"from {pkg}.cli import main\n\nraise SystemExit(main())\n",
        f"{pkg}/cli.py": render('''\
"""Command line entry point for {{name}}."""
import argparse

from . import __version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="{{slug}}", description="{{description}}")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args(argv)
    print("hello from {{slug}}")
    return 0
''', ctx),
        "tests/__init__.py": "",
        "tests/test_cli.py": render('''\
import unittest

from {{pkg}}.cli import main


class CliTest(unittest.TestCase):
    def test_runs(self):
        self.assertEqual(main([]), 0)


if __name__ == "__main__":
    unittest.main()
''', ctx),
    }


def build_c(ctx: dict) -> tuple:
    mk = (
        "CC      ?= cc\n"
        "CFLAGS  ?= -Wall -Wextra -std=c11 -O2\n"
        "TARGET  := {{slug}}\n"
        "SRC     := $(wildcard src/*.c)\n"
        "OBJ     := $(SRC:src/%.c=build/%.o)\n\n"
        "all: $(TARGET)\n\n"
        "$(TARGET): $(OBJ)\n\t$(CC) $(CFLAGS) -o $@ $^\n\n"
        "build/%.o: src/%.c | build\n\t$(CC) $(CFLAGS) -c $< -o $@\n\n"
        "build:\n\tmkdir -p build\n\n"
        "run: all\n\t./$(TARGET)\n\n"
        "clean:\n\trm -rf build $(TARGET)\n\n"
        ".PHONY: all run clean\n"
    )
    return [], {
        "Makefile": render(mk, ctx),
        "src/main.c": render('#include <stdio.h>\n\nint main(void) {\n    puts("hello from {{slug}}");\n    return 0;\n}\n', ctx),
    }


def build_go(ctx: dict) -> tuple:
    return [], {
        "go.mod": f"module {ctx['slug']}\n\ngo 1.22\n",
        "main.go": render('package main\n\nimport "fmt"\n\nfunc main() {\n\tfmt.Println("hello from {{slug}}")\n}\n', ctx),
    }


def build_rust(ctx: dict) -> tuple:
    return [], {
        "Cargo.toml": f'[package]\nname = "{ctx["slug"]}"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
        "src/main.rs": render('fn main() {\n    println!("hello from {{slug}}");\n}\n', ctx),
    }


def builtin_presets() -> dict:
    ps = [
        Preset("node", "Node.js server: public/ (empty index.html), server, routes, package.json",
               ["js", "ts"], build_node, install=["npm", "install"],
               options={"framework": ["express", "http"]},
               next_steps=["npm install", "npm run dev"], takes_port=True),
        Preset("static", "Static site: public/ with index.html, style.css, main.js",
               ["html"], build_static,
               next_steps=["python3 -m http.server 8000 -d {{public_dir}}"]),
        Preset("python", "Python package with CLI entry point and unittest tests",
               ["py"], build_python,
               next_steps=["python -m unittest discover -s tests", "python -m {{pkg}}"]),
        Preset("c", "C project with Makefile", ["c"], build_c, next_steps=["make run"]),
        Preset("go", "Go module with main.go", ["go"], build_go,
               install=["go", "mod", "tidy"], next_steps=["go run ."]),
        Preset("rust", "Rust binary crate (Cargo)", ["rust"], build_rust, next_steps=["cargo run"]),
    ]
    for p in ps:
        p.base = p.key
    return {p.key: p for p in ps}


def custom_preset(key: str, spec: dict, builtin: dict) -> Preset:
    if not isinstance(spec, dict):
        raise PyxyError(f"presets.{key} must be a mapping")
    base = None
    if spec.get("extends"):
        base = builtin.get(spec["extends"])
        if base is None:
            raise PyxyError(f"presets.{key}: unknown base preset '{spec['extends']}' "
                            f"(built-in: {', '.join(builtin)})")
    dirs_spec = spec.get("dirs") or []
    files_spec = spec.get("files") or {}
    if not isinstance(dirs_spec, list) or not isinstance(files_spec, dict):
        raise PyxyError(f"presets.{key}: 'dirs' must be a list and 'files' a mapping")

    def overlay(ctx, dirs, files):
        for d in dirs_spec:
            dirs.append(safe_rel(render(str(d), ctx), f"presets.{key}.dirs"))
        for k, v in files_spec.items():
            path = safe_rel(render(str(k), ctx), f"presets.{key}.files")
            if v is None:
                files.pop(path, None)
            else:
                files[path] = render(str(v), ctx)

    install = spec.get("install", base.install if base else None)
    if isinstance(install, str):
        install = shlex.split(install)
    langs = spec.get("languages")
    if isinstance(langs, str):
        langs = [langs]
    langs = [norm_lang(x) for x in langs] if langs else (list(base.languages) if base else ["generic"])
    nxt = spec.get("next", base.next_steps if base else [])
    return Preset(
        key=key, description=str(spec.get("description") or (f"{base.key} + customisations" if base else "Custom preset")),
        languages=langs, build=base.build if base else (lambda ctx: ([], {})),
        install=install or None, options=dict(base.options) if base else {},
        next_steps=list(nxt) if isinstance(nxt, list) else [str(nxt)],
        takes_port=base.takes_port if base else False, base=base.key if base else key,
        overlay=overlay, custom=True,
    )


def get_presets(cfg: dict) -> dict:
    presets = builtin_presets()
    base = dict(presets)
    for key, spec in (cfg.get("presets") or {}).items():
        presets[str(key)] = custom_preset(str(key), spec, base)
    return presets


def option_defaults(cfg: dict, preset: Preset) -> dict:
    out = {k: v[0] for k, v in preset.options.items()}
    conf = cfg.get("options") or {}
    for src in (preset.base, preset.key):
        for k, v in (conf.get(src) or {}).items():
            if k in preset.options and v in preset.options[k]:
                out[k] = v
    return out


def resolve_opts(cfg: dict, preset: Preset, given) -> dict:
    opts = option_defaults(cfg, preset)
    pairs = []
    if isinstance(given, dict):
        pairs = list(given.items())
    else:
        for item in given or []:
            if "=" not in item:
                raise PyxyError(f"--opt expects KEY=VALUE (got {item!r})")
            k, v = item.split("=", 1)
            pairs.append((k, v))
    for k, v in pairs:
        k, v = str(k).strip().lower(), str(v).strip().lower()
        if k not in preset.options:
            avail = f" (available: {', '.join(preset.options)})" if preset.options else " (this preset has no options)"
            raise PyxyError(f"preset '{preset.key}' has no option '{k}'{avail}")
        if v not in preset.options[k]:
            raise PyxyError(f"invalid value '{v}' for option '{k}' (choose from: {', '.join(preset.options[k])})")
        opts[k] = v
    return opts


def resolve_preset_lang(cfg: dict, presets: dict, key, lang):
    d = cfg["defaults"]
    if key is None and lang:
        dk = d.get("preset")
        if dk in presets and lang in presets[dk].languages:
            key = dk
        else:
            for k, p in presets.items():
                if lang in p.languages:
                    key = k
                    break
            else:
                known = sorted({lg for p in presets.values() for lg in p.languages})
                raise PyxyError(f"no preset supports language '{lang}' (available: {', '.join(known)})")
    key = key or d.get("preset") or "node"
    if key not in presets:
        raise PyxyError(f"unknown preset '{key}' (available: {', '.join(presets)})")
    p = presets[key]
    if lang is None:
        dl = norm_lang(d.get("language"))
        lang = dl if dl in p.languages else p.languages[0]
    if lang not in p.languages:
        raise PyxyError(f"preset '{key}' does not support language '{lang}' (supported: {', '.join(p.languages)})")
    return key, lang


# --------------------------------------------------------------------------- #
# Planning and writing
# --------------------------------------------------------------------------- #

@dataclass
class Plan:
    root: Path
    name: str
    preset: Preset
    lang: str
    opts: dict
    ctx: dict
    dirs: list
    files: dict

    def entries(self) -> list:
        """[(relative path, is_dir)] for tree display."""
        out = [(d, True) for d in self.dirs if d not in (".", "")]
        out += [(f, False) for f in self.files]
        return out


def make_plan(cfg: dict, *, name: str, parent, preset=None, lang=None, opts=None, port=None,
              author=None, license_=None, description=None) -> Plan:
    presets = get_presets(cfg)
    d, paths = cfg["defaults"], cfg["paths"]
    key, lang = resolve_preset_lang(cfg, presets, preset, norm_lang(lang))
    p = presets[key]
    opts = resolve_opts(cfg, p, opts)

    name = (name or "").strip()
    if not name or "\0" in name:
        raise PyxyError("project name must not be empty")
    root = (Path(parent).expanduser() / name).resolve()
    project = root.name
    if not project:
        raise PyxyError(f"cannot derive a project name from '{name}'")

    port = d.get("port", 3000) if port is None else port
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise PyxyError(f"port must be a number (got {port!r})") from None
    if not 1 <= port <= 65535:
        raise PyxyError("port must be between 1 and 65535")

    slug = slugify(project)
    ctx = {
        "name": project, "slug": slug, "pkg": pkgname(slug), "lang": lang,
        "lang_label": LANGS.get(lang, lang), "preset": key,
        "author": d.get("author", "") if author is None else author,
        "license": d.get("license", "") if license_ is None else license_,
        "description": (d.get("description") if description is None else description) or project,
        "year": datetime.date.today().year, "port": port, "opts": opts,
        "public_dir": paths["public_dir"], "src_dir": paths["src_dir"],
        "ts_src_dir": paths["ts_src_dir"], "out_dir": paths["out_dir"],
        "server_file": paths["server_file"], "routes_file": paths["routes_file"],
    }
    for k, v in opts.items():
        ctx[f"opt_{k}"] = v

    dirs, files = p.build(ctx)
    dirs = [safe_rel(x) for x in dirs]
    files = {safe_rel(k): v for k, v in files.items()}

    fc = cfg["files"]
    common: dict = {}
    if fc.get("readme", True):
        steps = [render(s, ctx) for s in p.next_steps]
        run = "\n## Getting started\n\n```sh\n" + "\n".join(steps) + "\n```\n" if steps else ""
        common["README.md"] = f"# {project}\n\n{ctx['description']}\n{run}"
    if fc.get("gitignore", True):
        common[".gitignore"] = render(GITIGNORE.get(lang, GITIGNORE["generic"]), ctx)
    if fc.get("license", True) and str(ctx["license"]).upper() == "MIT":
        common["LICENSE"] = render(MIT, ctx)
    if fc.get("editorconfig", False):
        common[".editorconfig"] = EDITORCONFIG
    for k, v in common.items():
        files.setdefault(k, v)
    if p.overlay:
        p.overlay(ctx, dirs, files)

    return Plan(root, project, p, lang, opts, ctx, sorted(set(dirs)), files)


def tree_lines(root_name: str, entries: list) -> list:
    g = glyphs()
    tree: dict = {}
    for path, is_dir in entries:
        node = tree
        parts = path.split("/")
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last and not is_dir:
                node.setdefault(part, None)
            else:
                nxt = node.get(part)
                if nxt is None:
                    nxt = node[part] = {}
                node = nxt
    lines = [root_name + "/"]

    def walk(node, prefix):
        items = sorted(node.items(), key=lambda kv: (kv[1] is None, kv[0].lower()))
        for i, (k, v) in enumerate(items):
            last = i == len(items) - 1
            lines.append(prefix + (g["l"] if last else g["t"]) + k + ("/" if v is not None else ""))
            if v is not None:
                walk(v, prefix + (g["sp"] if last else g["bar"]))

    walk(tree, "")
    return lines


def _run(cmd, cwd: Path, shell: bool = False) -> bool:
    try:
        return subprocess.run(cmd, cwd=str(cwd), shell=shell).returncode == 0
    except OSError:
        return False


def execute_plan(cfg: dict, plan: Plan, *, git: bool, install: bool, force: bool = False,
                 dry_run: bool = False, quiet: bool = False, record: bool = True) -> None:
    root, g = plan.root, glyphs()
    say = (lambda *a, **k: None) if quiet else print
    if root.exists() and not root.is_dir():
        raise PyxyError(f"{root} exists and is not a directory")
    nonempty = root.is_dir() and any(root.iterdir())
    if nonempty and not force and not dry_run:
        raise PyxyError(f"{root} already exists and is not empty (use --force to write into it)")

    tag = f"{plan.preset.key} {g['dot']} {LANGS.get(plan.lang, plan.lang)}"
    if plan.opts:
        tag += " " + g["dot"] + " " + ", ".join(f"{k}={v}" for k, v in plan.opts.items())

    if dry_run:
        say(paint("33;1", "dry run") + f" - nothing written ({tag})")
        if nonempty:
            say(paint("33", f"note: {root} is not empty; --force would be required"))
        say("\n".join(tree_lines(plan.name, plan.entries())))
        return

    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    for d in plan.dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, content in plan.files.items():
        target = (root / rel).resolve()
        if resolved_root != target and resolved_root not in target.parents:
            raise PyxyError(f"refusing to write outside the project: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if record:
        record_history(cfg, plan)

    say(f"\n{paint('32;1', g['ok'])} Created {paint('1', plan.name)}  ({tag})")
    say(paint("2", f"  {g['arrow']} {root}"))
    say("\n".join("  " + ln for ln in tree_lines(plan.name, plan.entries())))

    installed = False
    if git:
        if shutil.which("git") and not (root / ".git").exists():
            if _run(["git", "init", "-q"], root):
                say(paint("2", "  git repository initialised"))
        elif not shutil.which("git"):
            say(paint("33", "  git not found - skipped git init"))
    if install and plan.preset.install:
        cmd = plan.preset.install
        if shutil.which(cmd[0]):
            say(paint("2", f"  running: {' '.join(cmd)}"))
            installed = _run(cmd, root)
            if not installed:
                say(paint("33", f"  '{' '.join(cmd)}' failed - run it manually"))
        else:
            say(paint("33", f"  {cmd[0]} not found - skipped '{' '.join(cmd)}'"))
    for hook in (cfg.get("hooks") or {}).get("post") or []:
        cmd = render(str(hook), plan.ctx)
        say(paint("2", f"  hook: {cmd}"))
        if not _run(cmd, root, shell=True):
            say(paint("33", f"  hook failed: {cmd}"))

    steps = [render(s, plan.ctx) for s in plan.preset.next_steps]
    if installed and plan.preset.install:
        steps = [s for s in steps if s != " ".join(plan.preset.install)]
    try:
        rel_root = os.path.relpath(root)
    except ValueError:
        rel_root = str(root)
    say("\nNext:")
    for s in ([f"cd {shlex.quote(rel_root)}"] if rel_root != "." else []) + steps:
        say(f"  {paint('36', s)}")


# --------------------------------------------------------------------------- #
# TUI (curses)
# --------------------------------------------------------------------------- #

def complete_path(text: str):
    """Directory tab-completion. Returns (new_text, candidates, head)."""
    if text == "~":
        return "~/", [], "~/"
    if "/" in text:
        head, tail = text.rsplit("/", 1)
        head += "/"
    else:
        head, tail = "", text
    base = os.path.expanduser(head) if head else "."
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return text, [], head
    hidden = tail.startswith(".")
    cands = [n for n in names
             if n.startswith(tail) and (hidden or not n.startswith("."))
             and os.path.isdir(os.path.join(base, n))]
    if not cands:
        return text, [], head
    if len(cands) == 1:
        return head + cands[0] + "/", cands, head
    return head + os.path.commonprefix(cands), cands, head


def run_tui(cfg: dict):
    """Interactive form. Returns a dict of answers, or None if cancelled."""
    try:
        import curses
    except ImportError:
        raise PyxyError("the TUI needs the 'curses' module (Windows: pip install windows-curses)") from None
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    presets = get_presets(cfg)
    return curses.wrapper(lambda scr: _tui(scr, curses, cfg, presets))


def _tui(scr, curses, cfg, presets):
    d = cfg["defaults"]
    uni = _utf()
    left, right = ("‹ ", " ›") if uni else ("< ", " >")
    mark = "▸" if uni else ">"
    cursor = "█" if uni else "_"
    ell = "…" if uni else "..."
    dot = glyphs()["dot"]
    ud = "↑↓" if uni else "up/down"
    lr = "←→" if uni else "left/right"

    try:
        curses.curs_set(0)
    except curses.error:
        pass
    attrs = {"accent": curses.A_BOLD, "dim": curses.A_DIM, "ok": curses.A_BOLD,
             "err": curses.A_BOLD, "sel": curses.A_REVERSE, "hi": curses.A_BOLD}
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_CYAN, bg)
        curses.init_pair(2, curses.COLOR_GREEN, bg)
        curses.init_pair(3, curses.COLOR_RED, bg)
        curses.init_pair(4, curses.COLOR_MAGENTA, bg)
        attrs.update(accent=curses.color_pair(1) | curses.A_BOLD, ok=curses.color_pair(2) | curses.A_BOLD,
                     err=curses.color_pair(3) | curses.A_BOLD, hi=curses.color_pair(4) | curses.A_BOLD)

    st = {"preset": d.get("preset") if d.get("preset") in presets else next(iter(presets)),
          "language": None, "name": "my-project", "dir": default_parent(cfg),
          "port": str(d.get("port", 3000)), "git": bool(d.get("git", True)),
          "install": bool(d.get("install", False)), "opts": {}}
    loc_list: list = []

    def refresh_locs():
        loc_list[:] = [("here", ".")] + [(n, p) for n, p, _ in all_locations(cfg)]

    refresh_locs()

    def set_preset(key):
        st["preset"] = key
        p = presets[key]
        if st["language"] not in p.languages:
            dl = norm_lang(d.get("language"))
            st["language"] = dl if dl in p.languages else p.languages[0]
        st["opts"] = option_defaults(cfg, p)

    set_preset(st["preset"])

    def build_fields():
        p = presets[st["preset"]]
        fl = [dict(id="preset", label="Preset", kind="choice", values=list(presets)),
              dict(id="language", label="Language", kind="choice", values=p.languages),
              dict(id="name", label="Name", kind="text"),
              dict(id="dir", label="Location", kind="location")]
        for opt, vals in p.options.items():
            fl.append(dict(id="opt:" + opt, label=opt.capitalize(), kind="choice", values=vals))
        if p.takes_port:
            fl.append(dict(id="port", label="Port", kind="text"))
        fl.append(dict(id="git", label="git init", kind="toggle"))
        if p.install:
            fl.append(dict(id="install", label="Install deps", kind="toggle"))
        fl.append(dict(id="go", label="Generate", kind="button"))
        fl.append(dict(id="quit", label="Quit", kind="button"))
        return fl

    def val(f):
        return st["opts"].get(f["id"][4:]) if f["id"].startswith("opt:") else st[f["id"]]

    def setval(f, v):
        if f["id"] == "preset":
            set_preset(v)
        elif f["id"].startswith("opt:"):
            st["opts"][f["id"][4:]] = v
        else:
            st[f["id"]] = v

    def loc_name():
        for n, p in loc_list:
            if same_path(p, st["dir"]):
                return n
        return None

    def show(f):
        v = val(f)
        if f["kind"] == "toggle":
            return "[x] yes" if v else "[ ] no"
        if f["kind"] == "choice":
            label = LANGS.get(v, v) if f["id"] == "language" else str(v)
            return f"{left}{label}{right}"
        if f["kind"] == "location":
            n = loc_name()
            p = "." if v == "." else short_path(v)
            return f"{left}{n}{right} {p}" if n else p
        return str(v)

    def try_plan():
        try:
            return make_plan(cfg, name=st["name"] or "project", parent=st["dir"] or ".", preset=st["preset"],
                             lang=st["language"], opts=st["opts"], port=st["port"]), None
        except PyxyError as exc:
            return None, str(exc)

    def put(y, x, text, attr=0, maxw=None):
        h, w = scr.getmaxyx()
        if 0 <= y < h and 0 <= x < w - 1:
            n = w - x - 1 if maxw is None else min(w - x - 1, maxw)
            try:
                scr.addstr(y, x, text[:n], attr)
            except curses.error:
                pass

    def apply_entry(e):
        key = e.get("preset")
        if key not in presets:
            return f"preset '{key}' no longer exists", "err"
        set_preset(key)
        p = presets[key]
        if e.get("lang") in p.languages:
            st["language"] = e["lang"]
        for k, v in (e.get("opts") or {}).items():
            if k in p.options and v in p.options[k]:
                st["opts"][k] = v
        if e.get("port"):
            st["port"] = str(e["port"])
        st["dir"] = short_path(Path(os.path.expanduser(e["path"])).parent)
        return f"settings from {e.get('name', '?')} applied", "ok"

    def history_view():
        entries = load_history(cfg)
        cur, top, note = 0, 0, ""
        while True:
            scr.erase()
            h, w = scr.getmaxyx()
            put(1, 2, "History", attrs["accent"])
            put(1, 11, f"{len(entries)} project(s)", attrs["dim"])
            put(2, 2, "-" * min(w - 4, 70), attrs["dim"])
            rows = max(1, h - 7)
            if not entries:
                put(4, 2, "Nothing generated yet - projects you create show up here.", attrs["dim"])
            cur = max(0, min(cur, len(entries) - 1)) if entries else 0
            if cur < top:
                top = cur
            if cur >= top + rows:
                top = cur - rows + 1
            for r, e in enumerate(entries[top:top + rows]):
                i = top + r
                path = e.get("path", "")
                exists = os.path.isdir(os.path.expanduser(path))
                tag = f"{e.get('preset', '?')} {dot} {LANGS.get(e.get('lang'), e.get('lang', '?'))}"
                line = (f"{str(e.get('created', ''))[:10]}  {str(e.get('name', '?')):<18.18}  "
                        f"{tag:<26.26}  {short_path(path)}" + ("" if exists else "  (missing)"))
                attr = attrs["sel"] if i == cur else (0 if exists else attrs["dim"])
                put(4 + r, 2, line.ljust(max(0, w - 5)), attr, w - 4)
            if note:
                put(h - 3, 2, note, attrs["dim"])
            put(h - 2, 2, f"{ud} move  enter apply its settings to the form  x forget  q back", attrs["dim"])
            scr.refresh()
            try:
                ch = scr.get_wch()
            except curses.error:
                continue
            if ch == curses.KEY_RESIZE:
                continue
            note = ""
            if ch in ("q", "Q", "H", "\x1b"):
                return None
            elif ch in (curses.KEY_UP, "k"):
                cur -= 1
            elif ch in (curses.KEY_DOWN, "j"):
                cur += 1
            elif ch in (curses.KEY_HOME, "g"):
                cur = 0
            elif ch in (curses.KEY_END, "G"):
                cur = len(entries) - 1
            elif ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                if entries:
                    return entries[cur]
            elif ch in ("x", "X") and entries:
                forget_history(cfg, entries[cur].get("path", ""))
                entries = load_history(cfg)
                note = "forgotten (the folder itself was not touched)"

    sel, edit, buf, tab = 0, None, "", None      # edit: None | "text" | "path" | "locname"
    msg, msg_kind, confirm = "", "dim", None
    LABEL_W, VALUE_X, PX = 14, 18, 56

    while True:
        fields = build_fields()
        sel = max(0, min(sel, len(fields) - 1))
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 20 or w < 44:
            put(0, 0, "Terminal too small (need 44x20)", attrs["err"])
            scr.refresh()
            ch = scr.get_wch()
            if ch in ("q", "\x1b"):
                return None
            continue

        wide = w >= PX + 24
        valw = (PX if wide else w) - VALUE_X - 2
        put(1, 2, "PyxyProj", attrs["accent"])
        put(1, 11, f"v{__version__}  project scaffolder", attrs["dim"])
        put(2, 2, "-" * min(w - 4, 70), attrs["dim"])

        for i, f in enumerate(fields):
            y = 4 + i
            is_sel = i == sel
            if f["kind"] == "button":
                label = f"[ {f['label']} ]"
                attr = attrs["sel"] if is_sel else (attrs["ok"] if f["id"] == "go" else attrs["dim"])
                put(y + 1, 2, label, attr)
                continue
            here = bool(edit) and is_sel
            put(y, 0, mark if is_sel else " ", attrs["accent"])
            put(y, 2, ("Save as" if here and edit == "locname" else f["label"]).ljust(LABEL_W),
                attrs["hi"] if is_sel else 0)
            if here:
                put(y, VALUE_X, fit(buf, valw - 1) + cursor, attrs["accent"], valw)
            else:
                put(y, VALUE_X, fit(show(f), valw), attrs["accent"] if is_sel else 0, valw)

        plan, err = try_plan()
        if wide:
            put(3, PX, "Preview", attrs["hi"])
            if plan:
                for j, line in enumerate(tree_lines(plan.name, plan.entries())):
                    if 4 + j >= h - 6:
                        put(4 + j, PX, ell, attrs["dim"])
                        break
                    put(4 + j, PX, line)
            else:
                put(4, PX, err or "", attrs["err"])
        elif err and not msg:
            put(h - 3, 2, err, attrs["err"])

        if edit == "path" and tab and tab["cands"]:
            put(h - 5, 2, "tab:", attrs["dim"])
            x = 7
            for i, c in enumerate(tab["cands"]):
                label = c + "/"
                if x + len(label) >= w - 3:
                    put(h - 5, x, ell, attrs["dim"])
                    break
                put(h - 5, x, label, attrs["sel"] if i == tab["idx"] else attrs["accent"])
                x += len(label) + 2

        put(h - 4, 2, presets[st["preset"]].description, attrs["dim"])
        if msg:
            put(h - 3, 2, msg, attrs.get(msg_kind, 0))
        if edit == "path":
            hint = "type a path  tab complete (again: cycle)  ctrl-w up a level  enter accept  esc cancel"
        elif edit:
            hint = "type to edit  enter accept  esc cancel  ctrl-u clear"
        elif fields[sel]["kind"] == "location":
            hint = f"{lr} saved locations  enter edit path  a save this  x forget  H history  q quit"
        else:
            hint = f"{ud} move  {lr} change  enter edit/toggle  g generate  H history  q quit"
        put(h - 2, 2, hint, attrs["dim"])
        scr.refresh()

        try:
            ch = scr.get_wch()
        except curses.error:
            continue
        if ch == curses.KEY_RESIZE:
            continue
        f = fields[sel]
        msg = ""

        if edit:
            if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                text = buf.strip()
                if edit == "locname":
                    try:
                        add_saved_location(cfg, text, st["dir"])
                        refresh_locs()
                        msg, msg_kind = f"saved location '{text}'", "ok"
                    except PyxyError as exc:
                        msg, msg_kind = str(exc), "err"
                else:
                    setval(f, text)
                edit, tab = None, None
            elif ch == "\x1b":
                edit, tab = None, None
            elif ch == "\t":
                if edit == "path":
                    if tab and buf == tab["last"] and len(tab["cands"]) > 1:
                        tab["idx"] = (tab["idx"] + 1) % len(tab["cands"])
                        buf = tab["head"] + tab["cands"][tab["idx"]] + "/"
                        tab["last"] = buf
                    else:
                        buf, cands, head = complete_path(buf)
                        tab = {"cands": cands, "head": head, "idx": -1, "last": buf}
                        if not cands:
                            msg, msg_kind = "no matching folders", "dim"
            elif ch in ("\x7f", "\b") or ch == curses.KEY_BACKSPACE:
                buf, tab = buf[:-1], None
            elif ch == "\x15":
                buf, tab = "", None
            elif ch == "\x17":
                stripped = buf.rstrip("/")
                cut = stripped.rfind("/")
                buf, tab = (stripped[:cut + 1] if cut >= 0 else ""), None
            elif isinstance(ch, str) and ch.isprintable():
                buf, tab = buf + ch, None
            continue

        def cycle(step):
            if f["kind"] == "choice":
                vals = f["values"]
                cur = val(f)
                idx = vals.index(cur) if cur in vals else 0
                setval(f, vals[(idx + step) % len(vals)])
            elif f["kind"] == "toggle":
                setval(f, not val(f))
            elif f["kind"] == "location":
                paths = [p for _, p in loc_list]
                idx = next((i for i, p in enumerate(paths) if same_path(p, st["dir"])), None)
                idx = (0 if step > 0 else len(paths) - 1) if idx is None else (idx + step) % len(paths)
                st["dir"] = paths[idx]

        def generate():
            nonlocal msg, msg_kind, confirm
            if err or not plan:
                msg, msg_kind = err or "fix the form first", "err"
                return None
            force = False
            if plan.root.is_dir() and any(plan.root.iterdir()):
                if confirm != str(plan.root):
                    confirm = str(plan.root)
                    msg, msg_kind = f"{plan.root.name}/ is not empty - press g again to write into it", "err"
                    return None
                force = True
            return {"preset": st["preset"], "lang": st["language"], "name": st["name"], "dir": st["dir"] or ".",
                    "opts": dict(st["opts"]), "port": st["port"], "git": st["git"], "install": st["install"],
                    "force": force}

        if ch in ("q", "Q", "\x1b"):
            return None
        elif ch in (curses.KEY_UP, "k"):
            sel = (sel - 1) % len(fields)
        elif ch in (curses.KEY_DOWN, "j", "\t"):
            sel = (sel + 1) % len(fields)
        elif ch in (curses.KEY_LEFT, "h"):
            cycle(-1)
        elif ch in (curses.KEY_RIGHT, "l"):
            cycle(1)
        elif ch == "H":
            entry = history_view()
            if entry:
                msg, msg_kind = apply_entry(entry)
        elif ch == "a" and f["kind"] == "location":
            edit, tab = "locname", None
            buf = loc_name() or Path(os.path.expanduser(st["dir"])).resolve().name
        elif ch in ("x", "X") and f["kind"] == "location":
            n = loc_name()
            if n is None or n == "here":
                msg, msg_kind = "this folder is not a saved location", "dim"
            else:
                try:
                    remove_saved_location(cfg, n)
                    refresh_locs()
                    msg, msg_kind = f"forgot location '{n}'", "ok"
                except PyxyError as exc:
                    msg, msg_kind = str(exc), "err"
        elif ch in ("g", "G"):
            res = generate()
            if res:
                return res
        elif ch in ("\n", "\r", " ") or ch == curses.KEY_ENTER:
            if f["kind"] in ("choice", "toggle"):
                cycle(1)
            elif f["kind"] == "text":
                edit, buf, tab = "text", str(val(f)), None
            elif f["kind"] == "location":
                edit, buf, tab = "path", str(val(f)), None
            elif f["id"] == "go":
                res = generate()
                if res:
                    return res
            elif f["id"] == "quit":
                return None


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_new(cfg, a) -> int:
    d = cfg["defaults"]
    parent = a.dir or (resolve_location(cfg, a.loc) if a.loc else default_parent(cfg))
    plan = make_plan(cfg, name=a.name, parent=parent, preset=a.preset, lang=a.lang,
                     opts=a.opt, port=a.port, author=a.author, license_=a.license, description=a.description)
    execute_plan(cfg, plan, git=d.get("git", True) if a.git is None else a.git,
                 install=d.get("install", False) if a.install is None else a.install,
                 force=a.force, dry_run=a.dry_run, quiet=a.quiet, record=not a.no_history)
    return 0


def cmd_tui(cfg) -> int:
    answers = run_tui(cfg)
    if not answers:
        print("cancelled.")
        return 0
    plan = make_plan(cfg, name=answers["name"], parent=answers["dir"], preset=answers["preset"],
                     lang=answers["lang"], opts=answers["opts"], port=answers["port"])
    execute_plan(cfg, plan, git=answers["git"], install=answers["install"], force=answers["force"])
    return 0


def cmd_presets(cfg, a) -> int:
    presets = get_presets(cfg)
    want = norm_lang(a.lang)
    rows = [p for p in presets.values() if not want or want in p.languages]
    if not rows:
        raise PyxyError(f"no preset supports language '{a.lang}'")
    kw = max(len(p.key) for p in rows)
    lw = max(len(", ".join(p.languages)) for p in rows)
    for p in rows:
        opts = ""
        if p.options:
            opts = "  " + paint("2", "[" + "; ".join(f"{k}={'|'.join(v)}" for k, v in p.options.items()) + "]")
        mark = paint("35", " (custom)") if p.custom else ""
        print(f"  {paint('36;1', p.key.ljust(kw))}  {', '.join(p.languages).ljust(lw)}  {p.description}{mark}{opts}")
    print(paint("2", f"\ndefault preset: {cfg['defaults'].get('preset')}"))
    return 0


def _age_line(e: dict) -> str:
    return str(e.get("created", ""))[:16].replace("T", " ")


def cmd_history(cfg, a) -> int:
    entries = load_history(cfg)
    if a.clear:
        save_history(cfg, [])
        print(f"cleared {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
        return 0
    gone = [e for e in entries if not os.path.isdir(os.path.expanduser(e.get("path", "")))]
    if a.prune:
        save_history(cfg, [e for e in entries if e not in gone])
        print(f"forgot {len(gone)} project(s) whose folder no longer exists")
        return 0
    shown = gone if a.missing else entries
    if a.limit:
        shown = shown[: a.limit]
    if a.json:
        print(json.dumps(shown, indent=2, ensure_ascii=False))
        return 0
    if not shown:
        print("Nothing missing." if a.missing and entries else
              "No projects recorded yet - ones you generate with pyxyproj will be listed here.")
        return 0
    dot = glyphs()["dot"]
    nw = min(24, max(len(str(e.get("name", "?"))) for e in shown))
    tags = [f"{e.get('preset', '?')} {dot} {LANGS.get(e.get('lang'), e.get('lang', '?'))}" for e in shown]
    tw = max(len(t) for t in tags)
    for e, tag in zip(shown, tags):
        miss = e in gone
        path = short_path(e.get("path", ""))
        line = (f"  {paint('2', _age_line(e))}  {paint('36;1', str(e.get('name', '?')).ljust(nw))}  "
                f"{tag.ljust(tw)}  {paint('2' if not miss else '31', path + ('  (missing)' if miss else ''))}")
        print(line)
    print(paint("2", f"\n{len(shown)} of {len(entries)} project(s)  {glyphs()['dot']}  {history_path(cfg)}"))
    return 0


def cmd_locations(cfg, a) -> int:
    if a.action == "add":
        if not a.name:
            raise PyxyError("usage: pyxyproj locations add NAME [PATH]   (PATH defaults to the current folder)")
        stored = add_saved_location(cfg, a.name, a.path)
        print(f"{paint('32;1', glyphs()['ok'])} saved location {paint('1', a.name)} {glyphs()['arrow']} {stored}")
        return 0
    if a.action == "remove":
        if not a.name:
            raise PyxyError("usage: pyxyproj locations remove NAME")
        remove_saved_location(cfg, a.name)
        print(f"forgot location {a.name}")
        return 0
    rows = [("here", ".", "built-in")] + all_locations(cfg)
    nw = max(len(r[0]) for r in rows)
    for name, path, src in rows:
        real = os.path.expanduser(path)
        ok = os.path.isdir(real)
        note = "" if ok or name == "here" else paint("33", "  (folder does not exist yet)")
        print(f"  {paint('36;1', name.ljust(nw))}  {path.ljust(28)}  {paint('2', src)}{note}")
    default = cfg["defaults"].get("location")
    print(paint("2", f"\ndefault: {default if default else cfg['defaults'].get('output_dir', '.')}"
                     f"  {glyphs()['dot']}  use with: pyxyproj new NAME -L <location>"))
    return 0


def cmd_config(cfg_loader, a) -> int:
    if a.action == "path":
        print(f"user:  {user_config_path()}{'' if user_config_path().is_file() else '  (not created)'}")
        loc = local_config_path()
        print(f"local: {loc if loc else Path.cwd() / 'pyxyproj.yaml'}{'' if loc else '  (not created)'}")
        return 0
    if a.action == "init":
        target = user_config_path() if a.glob else Path.cwd() / "pyxyproj.yaml"
        if target.exists() and not a.force:
            raise PyxyError(f"{target} already exists (use --force to overwrite)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"{paint('32;1', glyphs()['ok'])} wrote {target}")
        return 0
    cfg, sources = cfg_loader()
    print(paint("2", "# sources: " + (", ".join(map(str, sources)) if sources else "built-in defaults")))
    print(dump_yaml(cfg))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=APP, description="Scaffold projects from the CLI or a TUI, driven by a YAML config.",
        epilog="Run without a command to open the TUI.")
    p.add_argument("-V", "--version", action="version", version=f"{APP} {__version__}")
    p.add_argument("-c", "--config", metavar="FILE", help="use only this config file")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    sub.add_parser("tui", help="open the interactive form")

    n = sub.add_parser("new", aliases=["create"], help="create a project")
    n.add_argument("name", help="project name or path (use '.' for the current folder)")
    n.add_argument("-p", "--preset", help="preset key (see `presets`)")
    n.add_argument("-l", "--lang", help="language: js, ts, py, c, go, rust, html")
    where = n.add_mutually_exclusive_group()
    where.add_argument("-d", "--dir", help="parent folder (default: defaults.output_dir)")
    where.add_argument("-L", "--loc", metavar="NAME", help="use a saved location (see `locations`)")
    n.add_argument("-o", "--opt", action="append", metavar="KEY=VALUE", help="preset option, e.g. -o framework=http")
    n.add_argument("--port", type=int, help="server port written into generated code")
    n.add_argument("--author")
    n.add_argument("--license")
    n.add_argument("--description")
    n.add_argument("--git", action=argparse.BooleanOptionalAction, default=None, help="run git init")
    n.add_argument("--install", action=argparse.BooleanOptionalAction, default=None, help="run the install step")
    n.add_argument("-f", "--force", action="store_true", help="write into a non-empty folder")
    n.add_argument("-n", "--dry-run", action="store_true", help="show what would be created")
    n.add_argument("-q", "--quiet", action="store_true")
    n.add_argument("--no-history", action="store_true", help="do not record this project in the history")

    ls = sub.add_parser("presets", aliases=["list", "ls"], help="list presets")
    ls.add_argument("-l", "--lang", help="only presets for this language")

    hs = sub.add_parser("history", aliases=["hist"], help="projects generated so far")
    hs.add_argument("-n", "--limit", type=int, help="show only the newest N")
    hs.add_argument("--missing", action="store_true", help="only projects whose folder no longer exists")
    hs.add_argument("--prune", action="store_true", help="forget entries whose folder no longer exists")
    hs.add_argument("--clear", action="store_true", help="forget everything (folders are not touched)")
    hs.add_argument("--json", action="store_true")

    lc = sub.add_parser("locations", aliases=["loc"], help="saved default locations")
    lc.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    lc.add_argument("name", nargs="?")
    lc.add_argument("path", nargs="?", help="add: folder (default: current)")

    c = sub.add_parser("config", help="show / locate / create the config file")
    c.add_argument("action", nargs="?", choices=["show", "path", "init"], default="show")
    c.add_argument("--global", dest="glob", action="store_true", help="init: write the user-level config")
    c.add_argument("-f", "--force", action="store_true", help="init: overwrite an existing file")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    try:
        if a.cmd == "config":
            return cmd_config(lambda: load_config(a.config), a)
        cfg, _ = load_config(a.config)
        if a.cmd is None:
            if sys.stdin.isatty() and sys.stdout.isatty():
                return cmd_tui(cfg)
            parser.print_help()
            return 0
        if a.cmd == "tui":
            return cmd_tui(cfg)
        if a.cmd in ("new", "create"):
            return cmd_new(cfg, a)
        if a.cmd in ("presets", "list", "ls"):
            return cmd_presets(cfg, a)
        if a.cmd in ("history", "hist"):
            return cmd_history(cfg, a)
        if a.cmd in ("locations", "loc"):
            return cmd_locations(cfg, a)
    except PyxyError as exc:
        print(f"{paint('31;1', 'error:')} {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # e.g. `pyxyproj history | head`
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
