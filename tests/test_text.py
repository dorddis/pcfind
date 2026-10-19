import os

NEEDLE = "qq-needle-7731"


def test_footer_discloses_filters_on_a_non_empty_result(pcfind, tree):
    tree.put("notes/a.md", f"one {NEEDLE}\n")
    r = pcfind("text", NEEDLE, "--root", tree)
    assert r.returncode == 0
    assert len(r.lines) == 1 and r.lines[0].endswith(f"a.md:1:one {NEEDLE}")
    assert "-- 1 matching lines" in r.stderr
    assert "-- filters active:" in r.stderr
    assert "dead-zone dirs skipped" in r.stderr
    assert "credential filename patterns skipped" in r.stderr
    assert "binary extensions and files >20M skipped" in r.stderr


def test_no_match_exits_1_and_missing_root_exits_2(pcfind, tree):
    tree.put("a.txt", "nothing here\n")
    assert pcfind("text", NEEDLE, "--root", tree).returncode == 1
    r = pcfind("text", NEEDLE, "--root", tree / "does-not-exist")
    assert r.returncode == 2
    assert "skipping missing root" in r.stderr


def test_gitignored_and_hidden_files_are_searched(pcfind, tree):
    tree.put(".git/HEAD", "ref: refs/heads/main\n")
    tree.put(".gitignore", "private/\n")
    tree.put("private/.notes/log.md", f"{NEEDLE}\n")
    r = pcfind("text", NEEDLE, "--root", tree)
    assert r.returncode == 0 and len(r.lines) == 1
    r = pcfind("text", NEEDLE, "--root", tree, "--respect-gitignore")
    assert r.returncode == 1
    assert "gitignore honoured" in r.stderr


def test_secrets_skipped_by_default_and_included_on_request(pcfind, tree):
    tree.put("app/.env", f"KEY={NEEDLE}\n")
    tree.put("app/server.pem", f"{NEEDLE}\n")
    tree.put("app/.ssh/config", f"{NEEDLE}\n")
    r = pcfind("text", NEEDLE, "--root", tree)
    assert r.returncode == 1 and r.lines == []
    r = pcfind("text", NEEDLE, "--root", tree, "--include-secrets")
    assert r.returncode == 0 and len(r.lines) == 3
    assert "credential filename patterns" not in r.stderr


def test_deadzone_skipped_and_widened(pcfind, tree):
    tree.put("node_modules/pkg/index.js", f"{NEEDLE}\n")
    tree.put("src/main.js", f"{NEEDLE}\n")
    r = pcfind("text", NEEDLE, "--root", tree)
    assert [l for l in r.lines if "node_modules" in l] == []
    assert len(r.lines) == 1
    r = pcfind("text", NEEDLE, "--root", tree, "--no-deadzones")
    assert len(r.lines) == 2


def test_file_named_like_a_deadzone_is_still_searched(pcfind, tree):
    tree.put("node_modules", f"{NEEDLE}\n")
    r = pcfind("text", NEEDLE, "--root", tree)
    assert len(r.lines) == 1


def test_root_inside_a_deadzone_lifts_it_and_says_so(pcfind, tree):
    tree.put("node_modules/pkg/index.js", f"{NEEDLE}\n")
    r = pcfind("text", NEEDLE, "--root", tree / "node_modules" / "pkg")
    assert len(r.lines) == 1
    assert "dead-zone node_modules SEARCHED because the root is inside it" in r.stderr


def test_config_and_flag_deadzones(pcfind, tree, tmp_path):
    tree.put("bigcache/x.txt", f"{NEEDLE}\n")
    tree.put("scratch-01/x.txt", f"{NEEDLE}\n")
    tree.put("keep/x.txt", f"{NEEDLE}\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text('deadzones = ["bigcache"]\n', encoding="utf-8")
    r = pcfind("text", NEEDLE, "--root", tree, config=cfg)
    assert len(r.lines) == 2
    r = pcfind("text", NEEDLE, "--root", tree, "--deadzone", "scratch-*", config=cfg)
    assert len(r.lines) == 1 and "keep" in r.lines[0]


def test_skipped_extension_any_case_and_deep(pcfind, tree):
    tree.put("docs/report.pdf", f"text {NEEDLE}\n")
    tree.put("docs/SCAN.PDF", f"text {NEEDLE}\n")
    tree.put("docs/data.bin", f"text {NEEDLE}\n")
    r = pcfind("text", NEEDLE, "--root", tree)
    assert len(r.lines) == 1 and r.lines[0].split(":")[-3].endswith("data.bin")
    r = pcfind("text", NEEDLE, "--root", tree, "--deep")
    assert len(r.lines) == 3
    assert "binary extensions" not in r.stderr


def test_thin_result_warning(pcfind, tree):
    tree.put("one.txt", f"{NEEDLE}\n")
    r = pcfind("text", NEEDLE, "--root", tree)
    assert "WARNING: only 1 match(es)" in r.stderr
    tree.put("many.txt", f"{NEEDLE}\n" * 6)
    r = pcfind("text", NEEDLE, "--root", tree)
    assert "WARNING" not in r.stderr
    r = pcfind("text", "absent-zz-1", "--root", tree, "--deep", "--include-secrets",
               "--no-deadzones")
    assert r.returncode == 1
    assert "WARNING" not in r.stderr and "filters active" not in r.stderr


def test_pattern_that_looks_like_a_flag(pcfind, tree):
    tree.put("cli.md", "run it with --help for usage\n")
    r = pcfind("text", "--root", tree, "--", "--help")
    assert r.returncode == 0
    assert len(r.lines) == 1 and r.lines[0].endswith("run it with --help for usage")
    assert "USAGE" not in r.stdout and "ripgrep" not in r.stdout


def test_binary_notice_suppressed_for_explicit_file_root(pcfind, tree):
    f = tree.put("blob.dat", b"head\x00\x00 " + NEEDLE.encode() + b"\n")
    r = pcfind("text", NEEDLE, "--root", f)
    assert "binary file matches" not in r.stdout.lower()
    assert r.lines == []
    assert "1 binary file(s) passed as a root matched" in r.stderr
    r = pcfind("text", NEEDLE, "--root", tree)
    assert "binary file matches" not in r.stdout.lower() and r.lines == []


def test_control_characters_are_escaped(pcfind, tree):
    tree.put("evil.txt", f"{NEEDLE} \x1b[2J gone\n")
    r = pcfind("text", NEEDLE, "--root", tree)
    assert "\x1b" not in r.stdout
    assert "\\x1b[2J" in r.stdout
    assert "escaped before printing" in r.stderr


def test_default_root_is_cwd_without_config(pcfind, tree):
    tree.put("here.txt", f"{NEEDLE}\n")
    r = pcfind("text", NEEDLE, cwd=tree)
    assert r.returncode == 0 and len(r.lines) == 1


def test_config_roots_are_the_default(pcfind, tree, tmp_path):
    tree.put("a/x.txt", f"{NEEDLE}\n")
    tree.put("b/x.txt", f"{NEEDLE}\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'roots = ["{(tree / "a").as_posix()}"]\n', encoding="utf-8")
    r = pcfind("text", NEEDLE, config=cfg)
    assert len(r.lines) == 1 and f"{os.sep}a{os.sep}" in r.lines[0]


def test_explicit_missing_config_is_an_error(pcfind, tree):
    r = pcfind("text", NEEDLE, "--root", tree, "--config", tree / "nope.toml")
    assert r.returncode != 0
    assert "config file not found" in r.stderr


def test_bad_regex_is_a_failure_not_a_zero(pcfind, tree):
    tree.put("a.txt", "x\n")
    r = pcfind("text", "([unclosed", "--root", tree)
    assert r.returncode == 2


def test_name_with_root_traverses(pcfind, tree):
    tree.put("logs/app.log", "x\n")
    tree.put("logs/.env", "x\n")
    tree.put("node_modules/dep.log", "x\n")
    r = pcfind("name", "*.log", "--root", tree)
    assert r.returncode == 0 and len(r.lines) == 1 and r.lines[0].endswith("app.log")
    assert "via ripgrep traversal" in r.stderr
    r = pcfind("name", ".env", "--root", tree)
    assert len(r.lines) == 1
