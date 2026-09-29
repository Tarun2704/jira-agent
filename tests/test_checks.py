from app.checks import CheckResult, format_checks_md, python_syntax_check
from app.pr_writer import build_pr_body
from tests.test_pr_writer import ISSUE


def test_syntax_passed_skipped_and_failed(tmp_path):
    (tmp_path / "ok.py").write_text("def f():\n    return 1\n")
    (tmp_path / "bad.py").write_text("def f(:\n    return 1\n")
    (tmp_path / "notes.md").write_text("# hi")

    ok = python_syntax_check(tmp_path, ["ok.py", "notes.md"])
    assert ok.status == "passed" and "`ok.py`" in ok.summary

    assert python_syntax_check(tmp_path, ["notes.md"]).status == "skipped"
    assert python_syntax_check(tmp_path, ["deleted.py"]).status == "skipped"  # not on disk

    bad = python_syntax_check(tmp_path, ["ok.py", "bad.py"])
    assert bad.status == "failed" and bad.failed_files == ["bad.py"]
    assert "line 1" in bad.errors[0]


def test_check_writes_no_pyc(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    python_syntax_check(tmp_path, ["a.py"])
    assert not (tmp_path / "__pycache__").exists()


def test_windows_line_endings_compile(tmp_path):
    (tmp_path / "win.py").write_bytes(b"x = 1\r\nif x:\r\n    y = 2\r\n")
    assert python_syntax_check(tmp_path, ["win.py"]).status == "passed"


def test_format_checks_md():
    passed = CheckResult("Python syntax", "passed", "1 changed file(s) compile.", retried=True)
    md = format_checks_md([passed])
    assert md.startswith("## Checks") and "✅" in md and "fixed a syntax error" in md

    failed = CheckResult("Python syntax", "failed", "Syntax errors in 1 of 1 file(s).", ["a.py: line 3: invalid syntax"])
    md = format_checks_md([failed])
    assert "❌" in md and "a.py: line 3" in md and "Do not merge" in md


def test_pr_body_includes_checks_before_footer():
    body = build_pr_body(
        issue=ISSUE, jira_url="u", description="## Summary\nx", diff_stat="s", agent_log="l",
        checks_md="## Checks\n- ✅ ok",
    )
    assert body.index("## Checks") < body.index("---\n**Jira:**")
