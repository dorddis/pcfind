r"""Find a string or a filename anywhere on a machine, without the search silently skipping the
places your own notes live, and without it taking minutes.

  pcfind text "parseConfigFile"          content search, default roots
  pcfind text "foo" --root ~/some/dir        content search, one root
  pcfind name "*.pdf"                        filename search (whole PC on Windows, via Everything)
  pcfind doctor                              rank every top-level dir by search cost

Two defaults are deliberate and opposite to a code-search tool's:

  --no-ignore is ON. ripgrep honours .gitignore by default, and the files people search for
  most - notes, drafts, logs, local scratch - are exactly the ones they gitignore. Use
  --respect-gitignore to opt back in.

  --hidden is ON. Dot-directories hold caches, logs and tool state that are worth finding.

Every filter that could hide a result is disclosed in the footer. A search that returns nothing
because everything was excluded must never look like a search that returned nothing because
nothing is there.

A matched line is attacker-controlled text: planting one readable file is enough to put chosen
bytes on this tool's stdout, which may be read back into an AI agent's context and rendered in a
terminal. Control bytes and bidi marks are escaped on the way out. The `path:lineno:` prefix is
the only boundary between tool framing and file content; nothing to the right of it is
trustworthy.

Credential-shaped FILENAMES (.env, *.pem, id_rsa, .ssh/) are excluded from `text` by default,
because --no-ignore plus --hidden otherwise make them ordinary in-scope content.
--include-secrets opts back in. `name` is deliberately NOT filtered: a path is not a value, and
a name search for .env must not answer zero.

Scope is not a security boundary. --root reads any path the caller can read, `name` queries the
whole drive through Everything, and the exit code alone is a string-existence oracle.

Exit codes: 0 = matches found, 1 = clean run with no matches, 2 = something failed or was
degraded (bad pattern, unreadable root, timeout, missing root). A failed run never exits 1.

Writes nothing except stdout, stderr and one cache marker file (see `name`).
"""

import argparse
import concurrent.futures as cf
import fnmatch
import glob
import io
import os
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from shutil import which

from . import __version__

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

IS_WINDOWS = os.name == "nt"
HOME = Path.home()

# Directory names never worth searching. Kept short on purpose: every name here is a potential
# silent false negative, which is why ordinary words like `build` and `dist` are absent. Add
# your own in the config file or with --deadzone.
BUILTIN_DEADZONES = [
    ".tmp.driveupload", ".tmp.drivedownload",
    "node_modules", ".git", ".venv", "venv", "__pycache__", "site-packages",
    ".next", ".turbo", ".parcel-cache", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    # A super-repo with a few hundred worktree checkouts is mostly duplicates of the same files;
    # search one checkout with --root instead.
    "worktrees", "_worktrees", ".worktrees", "_backups",
    "target",
    # Volume-root system dirs. Walking into the first is always ACL-denied on Windows, which
    # turns every search of a mounted volume into a failed run.
    "System Volume Information", "$RECYCLE.BIN",
]

# Applied via --type-add/--type-not, not globs: a glob like `!*.zip` also matches a DIRECTORY
# named assets.zip and drops its whole subtree. Type filters only ever match files.
SKIP_EXT = [
    "png", "jpg", "jpeg", "gif", "webp", "ico", "bmp", "pdf", "heic", "tif", "tiff", "psd",
    "mp3", "mp4", "mov", "avi", "mkv", "wav", "m4a", "webm", "ogg", "flac", "aac", "opus",
    "zip", "gz", "tar", "7z", "rar", "exe", "dll", "so", "dylib", "jar", "whl", "apk", "ipa",
    "pth", "ckpt", "npz", "npy", "pack", "idx", "woff", "woff2", "ttf", "otf", "eot",
    "xlsx", "docx", "pptx", "sst", "ldb", "dill", "wasm", "pak", "pyc", "pyd", "class", "dex",
    # Not "bin" or "svg": both are often plain text, and skipping .bin lost a planted hit in the
    # benchmark.
    "o", "obj", "lib", "node", "etl", "snapshot", "bundle", "vhdx", "iso",
]

MAX_FILESIZE = "20M"
THIN_RESULT = 5

# Matched on names, never on content, so this filter cannot itself leak what it protects.
SECRET_GLOBS = [
    "**/.env", "**/.env.*", "**/*.env",
    "**/.credentials.json", "**/credentials.json",
    "**/id_rsa*", "**/id_ed25519*", "**/id_ecdsa*", "**/id_dsa*",
    "**/*.pem", "**/*.pfx", "**/*.p12", "**/*.key", "**/*.keystore", "**/*.jks",
    "**/.npmrc", "**/.pypirc", "**/.netrc", "**/_netrc", "**/.pgpass", "**/.htpasswd",
    "**/.ssh/**", "**/.secrets/**", "**/.aws/credentials", "**/.gnupg/**",
]

BUILTIN_STALE_COPY = ["_backups", "$recycle.bin"]

# ESC[2J erases the screen and scrollback, a bare \r overwrites the path prefix just printed,
# and U+202E makes evil<U+202E>txt.exe render as evilexe.txt. Tab is left alone.
_ESCAPES = {c: f"\\x{c:02x}"
            for c in list(range(0x20)) + [0x7f] + list(range(0x80, 0xa0)) if c != 0x09}
_ESCAPES.update({c: f"\\u{c:04x}" for c in
                 list(range(0x200b, 0x2010)) + list(range(0x202a, 0x202f))
                 + list(range(0x2066, 0x206a)) + [0xfeff]})

# rg prints this for a binary file it was handed explicitly, and nothing for the same file found
# by walking. Suppressed so a file root and a directory root report the same lines.
_BINARY_NOTICE = re.compile(
    r"^(?:(?P<a>.+): binary file matches|Binary file (?P<b>.+) matches) "
    r"\(found .* byte around offset \d+\)$")

ES_TIMEOUT = 8
RG_THREADS = 16


def _env_dir(var, *fallback):
    v = os.environ.get(var)
    return Path(v) if v else HOME.joinpath(*fallback)


def config_dir():
    if IS_WINDOWS:
        return _env_dir("APPDATA", "AppData", "Roaming") / "pcfind"
    return _env_dir("XDG_CONFIG_HOME", ".config") / "pcfind"


def cache_dir():
    if IS_WINDOWS:
        return _env_dir("LOCALAPPDATA", "AppData", "Local") / "pcfind"
    return _env_dir("XDG_CACHE_HOME", ".cache") / "pcfind"


def es_dead_marker():
    return cache_dir() / "es_timeout"


CONFIG_KEYS = {"roots": list, "deadzones": list, "stale_copy_dirs": list, "rg": str, "es": str}


def load_config(explicit=None):
    """Returns (config dict, path or None). A missing default config is not an error."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SystemExit(f"pcfind: config file not found: {path}")
    else:
        env = os.environ.get("PCFIND_CONFIG")
        path = Path(env).expanduser() if env else config_dir() / "config.toml"
        if not path.is_file():
            return {}, None
    if tomllib is None:
        raise SystemExit("pcfind: reading a config file on Python < 3.11 needs `pip install tomli`")
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as e:
        raise SystemExit(f"pcfind: cannot read config {path}: {e}")
    cfg = {}
    for k, v in data.items():
        want = CONFIG_KEYS.get(k)
        if want is None:
            print(f"pcfind: warning: unknown config key '{k}' in {path}", file=sys.stderr)
        elif not isinstance(v, want) or (want is list and not all(isinstance(x, str) for x in v)):
            raise SystemExit(f"pcfind: config key '{k}' in {path} must be a "
                             f"{'list of strings' if want is list else 'string'}")
        else:
            cfg[k] = v
    return cfg, path


def sanitize(line):
    return line.translate(_ESCAPES)


def child_env():
    # rg reads flags from RIPGREP_CONFIG_PATH, and a `--pre=CMD` line there runs a binary per
    # searched file. --no-config already ignores it; dropping it covers any future rg call too.
    e = os.environ.copy()
    e.pop("RIPGREP_CONFIG_PATH", None)
    return e


_PLAT_TOKEN = re.compile(r"(x64|arm64|ia32)-(win32|linux|darwin)|(win32|linux|darwin)-(x64|arm64|ia32)")


def _runs_here(path):
    """Reject a vendored binary built for another OS or CPU (bundles ship several)."""
    m = _PLAT_TOKEN.search(path.replace("\\", "/").lower())
    if not m:
        return True
    arch = m.group(1) or m.group(4)
    osname = m.group(2) or m.group(3)
    want_os = {"win32": "win32", "linux": "linux", "darwin": "darwin"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    want_arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return osname == want_os and arch == want_arch


def vendored_rg_candidates():
    exe = "rg.exe" if IS_WINDOWS else "rg"
    h = str(HOME)
    cc = "@anthropic-ai/claude-code/vendor/ripgrep/*/" + exe
    vs = ["resources/app/node_modules*/@vscode/ripgrep*/bin/" + exe,
          "resources/app/node_modules*/@vscode/ripgrep*/bin/*/" + exe,
          "*/resources/app/node_modules*/@vscode/ripgrep*/bin/" + exe,
          "*/resources/app/node_modules*/@vscode/ripgrep*/bin/*/" + exe]
    if IS_WINDOWS:
        appdata = str(_env_dir("APPDATA", "AppData", "Roaming"))
        local = str(_env_dir("LOCALAPPDATA", "AppData", "Local"))
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pats = [f"{appdata}/npm/node_modules/{cc}",
                f"{h}/.bun/install/global/node_modules/{cc}",
                f"{h}/.bun/install/cache/@anthropic-ai/claude-*/vendor/ripgrep/*/{exe}"]
        for app_dir in (f"{local}/Programs/Microsoft VS Code", f"{local}/Programs/cursor",
                        f"{pf}/Microsoft VS Code"):
            pats += [f"{app_dir}/{v}" for v in vs]
    else:
        pats = [f"{h}/.npm-global/lib/node_modules/{cc}",
                f"{h}/.local/lib/node_modules/{cc}",
                f"/usr/local/lib/node_modules/{cc}",
                f"/opt/homebrew/lib/node_modules/{cc}",
                f"/usr/lib/node_modules/{cc}",
                f"{h}/.bun/install/global/node_modules/{cc}",
                f"{h}/.bun/install/cache/@anthropic-ai/claude-*/vendor/ripgrep/*/{exe}",
                f"{h}/.vscode-server/bin/*/node_modules/@vscode/ripgrep/bin/{exe}"]
        for app_dir in ("/Applications/Visual Studio Code.app/Contents/Resources/app",
                        f"{h}/Applications/Visual Studio Code.app/Contents/Resources/app",
                        "/usr/share/code/resources/app", "/usr/lib/code/resources/app"):
            pats += [f"{app_dir}/node_modules*/@vscode/ripgrep*/bin/{exe}",
                     f"{app_dir}/node_modules*/@vscode/ripgrep*/bin/*/{exe}"]
    found = []
    for pat in pats:
        for p in glob.glob(pat):
            if _runs_here(p) and os.path.isfile(p) and os.access(p, os.X_OK):
                found.append(os.path.normpath(p))
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found


def find_rg(cfg):
    if cfg.get("rg"):
        p = Path(cfg["rg"]).expanduser()
        return str(p) if p.is_file() else None
    return which("rg") or next(iter(vendored_rg_candidates()), None)


def find_es(cfg):
    if not IS_WINDOWS:
        return None
    if cfg.get("es"):
        p = Path(cfg["es"]).expanduser()
        return str(p) if p.is_file() else None
    local = _env_dir("LOCALAPPDATA", "AppData", "Local")
    cands = [local / "Microsoft" / "WinGet" / "Links" / "es.exe",
             Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Everything" / "es.exe",
             Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Everything" / "es.exe",
             HOME / "scoop" / "shims" / "es.exe"]
    return next((str(c) for c in cands if c.is_file()), None) or which("es")


def default_roots(cfg):
    return cfg.get("roots") or [os.getcwd()]


def resolve_roots(raw, warn=True):
    """Expand, canonicalise, drop missing, de-duplicate, and drop roots nested in another.

    Resolving junctions and symlinks first is what stops one physical file being reported once
    per alias.
    """
    seen, out, missing = set(), [], []
    for r in raw:
        p = Path(os.path.expandvars(str(r))).expanduser()
        try:
            p = p.resolve(strict=True)
        except (OSError, RuntimeError):
            missing.append(str(r))
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    kept = []
    for p in out:
        if any(p != q and q in p.parents for q in out):
            if warn:
                print(f"pcfind: note: dropping root {p} (already covered by a parent root)",
                      file=sys.stderr)
            continue
        kept.append(p)
    if warn:
        for m in missing:
            print(f"pcfind: warning: skipping missing root {m}", file=sys.stderr)
    return kept, missing


def all_deadzones(a, cfg):
    extra = list(cfg.get("deadzones", [])) + list(getattr(a, "deadzone", None) or [])
    return BUILTIN_DEADZONES + [d for d in extra if d not in BUILTIN_DEADZONES]


def active_deadzones(root, deadzones):
    """Dead zones minus any the root itself sits at or beneath.

    An explicit --root into node_modules is the caller saying "search here"; answering zero
    because the path contains an excluded name would make the result depend on how the path is
    spelled. fnmatch, because entries may be wildcards.
    """
    parts = [p.lower() for p in Path(root).parts]
    return [d for d in deadzones if not any(fnmatch.fnmatch(p, d.lower()) for p in parts)]


def build_filters(root, deadzones, deep, no_deadzones, respect_gitignore, skip_secrets=False):
    """Returns (args, notes) - notes describe every filter that could hide a result."""
    args, notes = ["--no-config", "--hidden"], []
    if not respect_gitignore:
        args.append("--no-ignore")
    else:
        notes.append("gitignore honoured")
    if not no_deadzones:
        dz = active_deadzones(root, deadzones)
        # `!**/d/` prunes the directory itself (so an ACL-denied one is never opened) and leaves
        # a FILE named d alone. `!**/d/**` is still needed for a walk rooted inside a dead zone,
        # because globs never apply at a walk's depth 0.
        for d in dz:
            args += ["-g", f"!**/{d}/", "-g", f"!**/{d}/**"]
        if dz:
            notes.append(f"{len(dz)} dead-zone dirs skipped")
        off = [d for d in deadzones if d not in dz]
        if off:
            notes.append(f"dead-zone {', '.join(off)} SEARCHED because the root is inside it")
    if skip_secrets:
        for g in SECRET_GLOBS:
            args += ["-g", f"!{g}"]
        notes.append(f"{len(SECRET_GLOBS)} credential filename patterns skipped "
                     f"(--include-secrets to read them)")
    if not deep:
        # rg type globs are case-sensitive; `*.pdf` alone lets REPORT.PDF through.
        both = ["".join(f"[{c}{c.upper()}]" if c.isalpha() else c for c in e) for e in SKIP_EXT]
        args += ["--type-add", "pcfindskip:*.{" + ",".join(both) + "}", "--type-not", "pcfindskip"]
        args += ["--max-filesize", MAX_FILESIZE]
        notes.append(f"{len(SKIP_EXT)} binary extensions and files >{MAX_FILESIZE} skipped")
    return args, notes


def _is_binary_notice(line):
    m = _BINARY_NOTICE.match(line)
    return bool(m) and os.path.isfile(m.group("a") or m.group("b"))


def stream_search(argv, timeout, limit, drop_binary=False):
    """Run rg, keeping at most `limit` lines in memory while counting them all.

    Returns (kept, total, warnings, error, binary_notices).
    """
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=child_env())
    except OSError as e:
        return [], 0, None, f"could not start ripgrep: {e}", 0

    # newline="\n": universal-newline mode would split on a bare \r inside a matched line and
    # print the second half without its `path:lineno:` prefix, letting file content forge a line
    # of this tool's own framing.
    stdout = io.TextIOWrapper(p.stdout, encoding="utf-8", errors="replace", newline="\n")
    kept, total, binary = [], 0, 0
    deadline = time.time() + timeout if timeout else None

    # The read below blocks until a line arrives, so a slow search that matches nothing would
    # never reach an in-loop deadline check. The watchdog kills rg regardless.
    timed_out = threading.Event()
    watchdog = None
    if timeout:
        def _reap():
            timed_out.set()
            p.kill()
        watchdog = threading.Timer(timeout, _reap)
        watchdog.daemon = True
        watchdog.start()

    try:
        for line in stdout:
            line = line.rstrip("\r\n")
            if not line:
                continue
            if drop_binary and _is_binary_notice(line):
                binary += 1
                continue
            total += 1
            if len(kept) < limit:
                kept.append(line)
            if deadline and time.time() > deadline:
                p.kill()
                p.wait(timeout=5)
                return kept, total, None, f"TIMEOUT after {timeout}s (partial: {total} lines)", binary
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        return kept, total, None, f"TIMEOUT after {timeout}s (partial: {total} lines)", binary
    finally:
        if watchdog:
            watchdog.cancel()

    if timed_out.is_set():
        return kept, total, None, f"TIMEOUT after {timeout}s (partial: {total} lines)", binary

    stderr = (p.stderr.read() or b"").decode("utf-8", "replace").strip()
    # rg: 0 = matched, 1 = no match, >=2 = real failure. Reading 2 as "no matches" is how a
    # typo'd regex answers "not on your PC".
    if p.returncode >= 2 and not all_not_found(stderr):
        return kept, total, None, (stderr.splitlines() or ["ripgrep failed"])[0], binary
    return kept, total, stderr, None, binary


def all_not_found(stderr):
    """A dangling symlink or a file deleted mid-walk has no content behind it; not a failure."""
    lines = [l for l in stderr.splitlines() if l.strip()]
    return bool(lines) and all("(os error 2)" in l or "(os error 3)" in l for l in lines)


def _no_rg():
    sys.exit("pcfind: no ripgrep found. Install it (https://github.com/BurntSushi/ripgrep), put it "
             "on PATH, or set `rg = \"/path/to/rg\"` in the config file.")


def cmd_text(a, cfg):
    rg = find_rg(cfg)
    if not rg:
        _no_rg()
    roots, missing = resolve_roots(a.root or default_roots(cfg))
    if not roots:
        print("pcfind: no existing roots to search", file=sys.stderr)
        return 2
    deadzones = all_deadzones(a, cfg)
    all_notes = []

    # One rg walk per root. rg checks --max-filesize against directory-listing metadata, so an
    # oversized file is never opened; batching explicit paths instead pays process spawn per
    # batch, which dominated on a loaded machine.
    def one(root):
        filters, notes = build_filters(root, deadzones, a.deep, a.no_deadzones,
                                       a.respect_gitignore, skip_secrets=not a.include_secrets)
        all_notes.append(notes)
        argv = [rg, "--line-number", "--with-filename", "--color", "never",
                "-j", str(RG_THREADS)] + filters
        if not a.case_sensitive:
            argv.append("-i")
        if a.fixed:
            argv.append("-F")
        if a.context is not None:
            argv += ["-C", str(a.context)]
        if a.max_count is not None:
            argv += ["-m", str(a.max_count)]
        for g in a.glob or []:
            argv += ["-g", g]
        t0 = time.time()
        # "--" or a pattern like "--help" is parsed by rg as a flag and prints rg's help as hits.
        res = stream_search(argv + ["--", a.pattern, str(root)], a.timeout, a.limit,
                            drop_binary=True)
        return (root,) + res + (time.time() - t0,)

    with cf.ThreadPoolExecutor(max_workers=max(1, min(len(roots), 8))) as ex:
        results = list(ex.map(one, roots))

    shown = grand = escaped = binary_total = 0
    failed = False
    for root, kept, total, warn, err, binary, secs in results:
        binary_total += binary
        if err:
            print(f"pcfind: {root}: {sanitize(err)}", file=sys.stderr)
            failed = True
        if warn:
            lines = warn.splitlines()
            if all_not_found(warn):
                print(f"pcfind: {root}: {len(lines)} entries not found (dangling links or "
                      f"deleted mid-walk), skipped; first: {sanitize(lines[0])}", file=sys.stderr)
            else:
                for l in lines[:3]:
                    print(f"pcfind: {root}: {sanitize(l)}", file=sys.stderr)
                if len(lines) > 3:
                    print(f"pcfind: {root}: ... {len(lines) - 3} more", file=sys.stderr)
                failed = True
        grand += total
        for l in kept:
            if shown >= a.limit:
                break
            clean = sanitize(l)
            if clean != l:
                escaped += 1
            print(clean)
            shown += 1

    elapsed = max((r[-1] for r in results), default=0.0)
    tail = f", showing {shown}" if grand > shown else ""
    print(f"\n-- {grand} matching lines in {elapsed:.2f}s across {len(roots)} root(s){tail}",
          file=sys.stderr)
    if escaped:
        print(f"-- {escaped} line(s) carried control or bidi characters; escaped before printing",
              file=sys.stderr)
    if binary_total:
        print(f"-- {binary_total} binary file(s) passed as a root matched; not printed "
              f"(a directory walk skips binary files silently)", file=sys.stderr)
    notes = all_notes[0] if all_notes else []
    # Printed on every run, not only on zero: a filtered result that looks unfiltered is the
    # same silent failure as a broken pattern reporting no hits.
    if notes:
        print(f"-- filters active: {'; '.join(notes)}. Re-run with --deep, --include-secrets "
              f"and --no-deadzones to widen.", file=sys.stderr)
        # In the benchmark, agents read the line above as boilerplate and reported a near-empty
        # result with high confidence while the answer sat in a skipped file.
        if grand < THIN_RESULT:
            print(f"-- WARNING: only {grand} match(es) and the filters above were active. This "
                  f"does NOT show the pattern is absent from skipped files. Before reporting it "
                  f"absent or complete, re-run with --deep --include-secrets.", file=sys.stderr)
    if a.show_filters:
        print(f"-- ripgrep: {rg}", file=sys.stderr)
        print(f"-- config: {a.config_path or 'none'}", file=sys.stderr)
    if missing or failed:
        return 2
    return 0 if grand else 1


def _stale_copy_re(cfg):
    names = BUILTIN_STALE_COPY + [n for n in cfg.get("stale_copy_dirs", [])]
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(r"(^|[\\/])(" + alt + r")([\\/]|$)", re.IGNORECASE)


def _es_recently_dead(window=300):
    """Everything blocks IPC for minutes while it rebuilds its index; without this marker every
    `name` call during a rebuild pays the full ES_TIMEOUT before falling back."""
    try:
        return time.time() - es_dead_marker().stat().st_mtime < window
    except OSError:
        return False


def _mark_es_dead():
    try:
        m = es_dead_marker()
        m.parent.mkdir(parents=True, exist_ok=True)
        m.touch()
    except OSError:
        pass


NOT_WHOLE_PC = "this is NOT a whole-PC search"


def cmd_name(a, cfg):
    es = find_es(cfg)
    if es and not a.root and _es_recently_dead():
        print(f"pcfind: Everything timed out within the last 5 minutes (index likely still "
              f"rebuilding); going straight to traversal of default roots - {NOT_WHOLE_PC}. "
              f"Delete {es_dead_marker()} to retry immediately.", file=sys.stderr)
        es = None
    elif not es and not a.root:
        why = "Everything is Windows-only" if not IS_WINDOWS else "Everything (es.exe) not found"
        print(f"pcfind: {why}; traversing default roots only - {NOT_WHOLE_PC}", file=sys.stderr)
    if es and not a.root:
        try:
            p = subprocess.run([es, "-n", str(a.limit), "--", a.pattern],
                               capture_output=True, text=True, errors="replace",
                               timeout=ES_TIMEOUT)
            if p.returncode == 0:
                raw = [l for l in p.stdout.splitlines() if l.strip()]
                stale = _stale_copy_re(cfg)
                if a.all_copies:
                    out, hidden = raw, 0
                else:
                    out = [l for l in raw if not stale.search(l)]
                    hidden = len(raw) - len(out)
                for l in out:
                    print(sanitize(l))
                note = f"\n-- {len(out)} files via Everything (whole-PC index)"
                if hidden:
                    note += f"; {hidden} stale-copy hit(s) hidden - pass --all to show them"
                print(note, file=sys.stderr)
                return 0 if out else 1
            err = (p.stderr or "").strip().splitlines()
            reason = sanitize(err[0]) if err else f"exit {p.returncode}"
        except subprocess.TimeoutExpired:
            _mark_es_dead()
            reason = f"no response in {ES_TIMEOUT}s (index likely rebuilding)"
        except OSError as e:
            reason = str(e)
        print(f"pcfind: Everything unavailable ({reason}); falling back to traversal of "
              f"default roots only - {NOT_WHOLE_PC}", file=sys.stderr)

    rg = find_rg(cfg)
    if not rg:
        _no_rg()
    roots, missing = resolve_roots(a.root or default_roots(cfg))
    if not roots:
        print("pcfind: no existing roots to search", file=sys.stderr)
        return 2
    deadzones = all_deadzones(a, cfg)

    def one(root):
        # No secrets filter: `name` returns paths, not values, and a search for '.env' that
        # answered zero would be exactly the silent false negative this tool exists to avoid.
        filters, _ = build_filters(root, deadzones, True, a.no_deadzones, a.respect_gitignore)
        argv = [rg, "--files"] + filters + ["-g", a.pattern, "--", str(root)]
        return stream_search(argv, a.timeout, a.limit)

    n = total = 0
    failed = bool(missing)
    with cf.ThreadPoolExecutor(max_workers=max(1, min(len(roots), 8))) as ex:
        for kept, cnt, warn, err, _ in ex.map(one, roots):
            if err:
                print(f"pcfind: {sanitize(err)}", file=sys.stderr)
                failed = True
            total += cnt
            for l in kept:
                if n >= a.limit:
                    break
                print(sanitize(l))
                n += 1
    tail = f", showing {n}" if total > n else ""
    hint = "" if find_es(cfg) or not IS_WINDOWS else "  (install Everything for whole-PC name search)"
    print(f"\n-- {total} files via ripgrep traversal of {len(roots)} root(s){tail}{hint}",
          file=sys.stderr)
    if failed:
        return 2
    return 0 if total else 1


def cmd_doctor(a, cfg):
    """Rank top-level dirs by the cost of the search `text` actually runs (same filters).

    Measuring an unfiltered walk instead reports node_modules as the cost when its real
    contribution is zero, and misses hidden bulk one level down.
    """
    rg = find_rg(cfg)
    if not rg:
        _no_rg()
    print(f"ripgrep:    {rg}")
    print(f"everything: {find_es(cfg) or 'not found'}")
    print(f"config:     {a.config_path or 'none (looked for ' + str(config_dir() / 'config.toml') + ')'}")
    roots, _ = resolve_roots(a.root or default_roots(cfg)[:1])
    if not roots:
        print("pcfind: no existing roots", file=sys.stderr)
        return 2
    deadzones = all_deadzones(a, cfg)

    status = 0
    for root in roots:
        if not root.is_dir():
            print(f"pcfind: {root} is not a directory", file=sys.stderr)
            status = 2
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            print(f"pcfind: cannot list {root}: {e}", file=sys.stderr)
            status = 2
            continue

        dirs = [d for d in entries if d.is_dir()]
        unreadable = [d for d in entries if not d.is_dir() and d.is_symlink()]
        print(f"probing {len(dirs)} top-level dirs under {sanitize(str(root))}")

        def probe(d):
            nm = sanitize(d.name)
            # Dead zones come from the user's root, never the probed child; otherwise probing a
            # dead zone would lift its own exclusion.
            filters, _ = build_filters(root, deadzones, a.deep, a.no_deadzones, a.respect_gitignore)
            argv = [rg, "-l", "--no-messages"] + filters + ["--", "zzzz-pcfind-nomatch-zzzz", str(d)]
            t0 = time.time()
            try:
                subprocess.run(argv, capture_output=True, timeout=a.timeout, env=child_env())
                return nm, time.time() - t0, False
            except subprocess.TimeoutExpired:
                return nm, float(a.timeout), True
            except OSError:
                return nm, 0.0, True

        # Serial: concurrent probes contend for disk and the timings stop meaning anything.
        rows = [probe(d) for d in dirs]
        measured = [(n, s) for n, s, to in rows if not to]
        capped = [(n, s) for n, s, to in rows if to]
        total = sum(s for _, s in measured)
        # Process spawn alone is tens of ms and noisy; below the floor gets no verdict.
        floor = max(0.5, a.threshold / 10)
        print(f"{'dir':<34}{'secs':>9}   verdict")
        for name, secs, to in sorted(rows, key=lambda r: (-r[1], r[0])):
            if to:
                print(f"{name:<34}{'>' + format(secs, '.2f'):>9}s  UNMEASURED (hit --timeout)")
                continue
            if secs >= a.threshold:
                share = f" - {secs / total * 100:.0f}% of measured" if total else ""
                verdict = f"DEAD ZONE{share}"
            elif secs >= floor:
                verdict = "heavy"
            else:
                verdict = ""
            print(f"{name:<34}{secs:>8.2f}s   {verdict}")
        for d in unreadable:
            print(f"{sanitize(d.name):<34}{'-':>9}   SKIPPED (unreadable)")
        print(f"\nmeasured {len(measured)} dirs, {total:.2f}s total")
        if capped:
            print(f"{len(capped)} dir(s) hit the {a.timeout}s cap and are UNMEASURED: "
                  f"{', '.join(n for n, _ in capped)}")
            print("ranking is incomplete - re-run with a larger --timeout before trusting it")
            status = 2
        elif measured:
            worst, wsecs = max(measured, key=lambda r: r[1])
            if wsecs >= a.threshold and total:
                print(f"worst: {worst} at {wsecs:.1f}s ({wsecs / total * 100:.0f}% of measured cost)")
                print("to skip it, add its name to `deadzones` in the config file")
    return status


def nonneg(v):
    i = int(v)
    if i < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return i


def build_parser():
    ap = argparse.ArgumentParser(prog="pcfind", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"pcfind {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", action="append",
                        help="search root (repeatable); default: `roots` from the config file, "
                             "else the current directory")
    common.add_argument("--limit", type=nonneg, default=100, help="max lines/files printed")
    common.add_argument("--deep", action="store_true", help="do not skip binary types or large files")
    common.add_argument("--no-deadzones", action="store_true",
                        help="also search node_modules, .git, .venv and the other dead zones")
    common.add_argument("--deadzone", action="append", metavar="NAME",
                        help="extra directory name (or glob) to skip, on top of the config file")
    common.add_argument("--respect-gitignore", action="store_true",
                        help="honour .gitignore (OFF by default - it hides your own notes)")
    common.add_argument("--timeout", type=nonneg, default=120)
    common.add_argument("--config", help=f"config file (default: $PCFIND_CONFIG, else "
                                         f"{config_dir() / 'config.toml'})")

    t = sub.add_parser("text", parents=[common], help="search file CONTENTS")
    t.add_argument("pattern")
    t.add_argument("--case-sensitive", action="store_true")
    t.add_argument("-F", "--fixed", action="store_true", help="literal, not regex")
    t.add_argument("-C", "--context", type=nonneg, default=None)
    t.add_argument("-m", "--max-count", type=nonneg, default=None, help="max matches per file")
    t.add_argument("-g", "--glob", action="append", help="extra rg glob (repeatable)")
    t.add_argument("--include-secrets", action="store_true",
                   help="also read .env/*.pem/id_rsa/.ssh - skipped by default")
    t.add_argument("--show-filters", action="store_true", help="also print the binary and config used")
    t.set_defaults(func=cmd_text)

    n = sub.add_parser("name", parents=[common], help="search FILENAMES")
    n.add_argument("pattern", help="glob, e.g. '*.pdf' or '*invoice*'")
    n.add_argument("--all", dest="all_copies", action="store_true",
                   help="include stale-copy trees (_backups, recycle bin, and `stale_copy_dirs` "
                        "from the config); hidden by default and the hidden count is reported")
    n.set_defaults(func=cmd_name)

    d = sub.add_parser("doctor", parents=[common], help="rank top-level dirs by search cost")
    d.add_argument("--threshold", type=float, default=5.0, help="secs to call it a dead zone")
    d.set_defaults(func=cmd_doctor)
    return ap


def _utf8_streams():
    for s in (sys.stdout, sys.stderr):
        try:
            if s.isatty():
                s.reconfigure(errors="replace")
            else:
                s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _utf8_streams()
    a = build_parser().parse_args(argv)
    cfg, a.config_path = load_config(a.config)
    if a.cmd == "doctor" and a.timeout == 120:
        a.timeout = 180
    sys.exit(a.func(a, cfg) or 0)
