"""Tests for otto.test_validation — deterministic test quality checks."""

import textwrap
from pathlib import Path

import pytest

from otto.test_validation import TestWarning, validate_test_quality


@pytest.fixture
def project(tmp_path):
    """Create a minimal Python project with Click CLI."""
    pkg = tmp_path / "myapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text("from myapp.cli import main\nmain()")
    (pkg / "cli.py").write_text(textwrap.dedent("""
        import click

        @click.group()
        def main():
            pass

        @main.command()
        @click.argument("title")
        def add(title):
            click.echo(f"Added: {title}")

        @main.command()
        def list_cmd():
            click.echo("listing")

        @main.command()
        @click.argument("query")
        def search(query):
            click.echo(f"searching: {query}")
    """))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    return tmp_path


def _write_test(project, content):
    """Write a test file and return its path."""
    f = project / "tests" / "test_generated.py"
    f.write_text(textwrap.dedent(content))
    return f


# ---------------------------------------------------------------------------
# Check 1: CLI command validation
# ---------------------------------------------------------------------------

class TestCLICommandValidation:
    def test_correct_module_and_command(self, project):
        f = _write_test(project, """
            import subprocess, sys
            def test_add():
                subprocess.run([sys.executable, "-m", "myapp", "add", "hello"])
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and w.check == "cli_command"]
        assert len(errors) == 0

    def test_wrong_module_name(self, project):
        f = _write_test(project, """
            import subprocess, sys
            def test_add():
                subprocess.run([sys.executable, "-m", "wrongapp", "add", "hello"])
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and w.check == "cli_command"]
        assert len(errors) == 1
        assert "wrongapp" in errors[0].message

    def test_unknown_subcommand_is_warning(self, project):
        f = _write_test(project, """
            import subprocess, sys
            def test_foo():
                subprocess.run([sys.executable, "-m", "myapp", "nonexistent"])
        """)
        warnings = validate_test_quality(f, project)
        warns = [w for w in warnings if w.severity == "warning" and w.check == "cli_command"]
        assert len(warns) == 1
        assert "nonexistent" in warns[0].message

    def test_no_click_project_skips_check(self, tmp_path):
        """Library-only project with no CLI — check should not run."""
        pkg = tmp_path / "mylib"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("def compute(): return 42")
        (tmp_path / "tests").mkdir()
        f = tmp_path / "tests" / "test_x.py"
        f.write_text("import subprocess, sys\ndef test_x():\n    subprocess.run([sys.executable, '-m', 'wrong'])")
        warnings = validate_test_quality(f, tmp_path)
        cli_errors = [w for w in warnings if w.check == "cli_command"]
        assert len(cli_errors) == 0  # no CLI detected, check skipped

    def test_click_command_name_kwarg_is_recognized(self, tmp_path):
        """Decorator aliases like @main.command(name='list') are valid Click commands."""
        pkg = tmp_path / "namedcli"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "__main__.py").write_text("from namedcli.cli import main\nmain()")
        (pkg / "cli.py").write_text(textwrap.dedent("""
            import click

            @click.group()
            def main():
                pass

            @main.command(name="list")
            def list_cmd():
                click.echo("listing")
        """))
        (tmp_path / "tests").mkdir()
        f = tmp_path / "tests" / "test_cli.py"
        f.write_text(textwrap.dedent("""
            import subprocess, sys

            def test_list():
                subprocess.run([sys.executable, "-m", "namedcli", "list"])
        """))

        warnings = validate_test_quality(f, tmp_path)
        cli_warnings = [w for w in warnings if w.check == "cli_command"]
        assert len(cli_warnings) == 0


# ---------------------------------------------------------------------------
# Check 2: Assertion analysis
# ---------------------------------------------------------------------------

class TestAssertionAnalysis:
    def test_tautological_assert_true(self, project):
        f = _write_test(project, """
            def test_trivial():
                assert True
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and "Tautological" in w.message]
        assert len(errors) == 1

    def test_tautological_same_value(self, project):
        f = _write_test(project, """
            def test_trivial():
                assert 1 == 1
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and "Tautological" in w.message]
        assert len(errors) == 1

    def test_tautological_same_variable(self, project):
        f = _write_test(project, """
            def test_trivial():
                x = 5
                assert x == x
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and "Tautological" in w.message]
        assert len(errors) == 1

    def test_valid_assertion_no_error(self, project):
        f = _write_test(project, """
            import subprocess, sys
            def test_real():
                r = subprocess.run([sys.executable, "-m", "myapp", "add", "hi"], capture_output=True)
                assert r.returncode == 0
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and w.check == "assertion"]
        assert len(errors) == 0

    def test_unreachable_assertion(self, project):
        f = _write_test(project, """
            def test_unreachable():
                return
                assert False
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and "Unreachable" in w.message]
        assert len(errors) == 1

    def test_nested_helper_assertions_do_not_trigger_test_errors(self, project):
        f = _write_test(project, """
            def test_outer():
                def helper():
                    assert True

                rendered = str(1)
                assert rendered == "1"
        """)
        warnings = validate_test_quality(f, project)
        assertion_errors = [w for w in warnings if w.severity == "error" and w.check == "assertion"]
        assert len(assertion_errors) == 0


# ---------------------------------------------------------------------------
# Check 3: Import validation
# ---------------------------------------------------------------------------

class TestImportValidation:
    def test_missing_conftest_export(self, project):
        (project / "tests" / "conftest.py").write_text("def existing_fixture(): pass")
        f = _write_test(project, """
            from tests.conftest import nonexistent_helper
            def test_x():
                nonexistent_helper()
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and w.check == "import"]
        assert len(errors) == 1
        assert "nonexistent_helper" in errors[0].message

    def test_valid_conftest_import(self, project):
        (project / "tests" / "conftest.py").write_text("def run_app(): pass")
        f = _write_test(project, """
            from tests.conftest import run_app
            def test_x():
                run_app()
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and w.check == "import"]
        assert len(errors) == 0

    def test_missing_conftest_file(self, project):
        # No conftest.py exists
        conftest = project / "tests" / "conftest.py"
        if conftest.exists():
            conftest.unlink()
        f = _write_test(project, """
            from tests.conftest import something
            def test_x():
                something()
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and w.check == "import"]
        assert len(errors) == 1

    def test_valid_project_import_does_not_error(self, project):
        f = _write_test(project, """
            from myapp.cli import main

            def test_import():
                assert main is not None
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and w.check == "import"]
        assert len(errors) == 0


# ---------------------------------------------------------------------------
# Check 4: Anti-patterns
# ---------------------------------------------------------------------------

class TestAntiPatterns:
    def test_unconditional_skip_is_error(self, project):
        f = _write_test(project, """
            import pytest
            @pytest.mark.skip
            def test_skipped():
                assert False
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and w.check == "anti_pattern"]
        assert any("skip" in w.message.lower() for w in errors)

    def test_xfail_is_error(self, project):
        f = _write_test(project, """
            import pytest
            @pytest.mark.xfail
            def test_expected_fail():
                assert False
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and w.check == "anti_pattern"]
        assert any("xfail" in w.message.lower() for w in errors)

    def test_class_method_xfail_with_reason_is_error(self, project):
        f = _write_test(project, """
            import pytest

            class TestGenerated:
                @pytest.mark.xfail(reason="not implemented")
                def test_expected_fail(self):
                    assert False
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and w.check == "anti_pattern"]
        assert any("xfail" in w.message.lower() for w in errors)

    def test_sleep_is_warning(self, project):
        f = _write_test(project, """
            import time
            def test_slow():
                time.sleep(5)
                assert True
        """)
        warnings = validate_test_quality(f, project)
        warns = [w for w in warnings if w.severity == "warning" and "sleep" in w.message.lower()]
        assert len(warns) >= 1

    def test_monkeypatch_subprocess_is_error(self, project):
        f = _write_test(project, """
            from unittest.mock import patch
            def test_mocked():
                with patch("subprocess.run") as mock:
                    mock.return_value = None
                    assert True
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error" and "subprocess" in w.message.lower()]
        assert len(errors) >= 1

    def test_monkeypatch_subprocess_via_aliased_patch_is_error(self, project):
        f = _write_test(project, """
            from unittest.mock import patch as mock_patch

            def test_mocked():
                with mock_patch("subprocess.run") as mock:
                    mock.return_value = None
                    assert True
        """)
        warnings = validate_test_quality(f, project)
        errors = [
            w for w in warnings
            if w.severity == "error" and w.check == "anti_pattern" and "subprocess" in w.message.lower()
        ]
        assert len(errors) >= 1

    def test_clean_test_no_anti_patterns(self, project):
        f = _write_test(project, """
            import subprocess, sys
            def test_clean():
                r = subprocess.run([sys.executable, "-m", "myapp", "add", "task1"], capture_output=True, text=True)
                assert r.returncode == 0
                assert "Added" in r.stdout
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error"]
        assert len(errors) == 0


# ---------------------------------------------------------------------------
# Check 5: Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_proper_naming_no_warning(self, project):
        f = project / "tests" / "test_something.py"
        f.write_text("def test_x(): assert True")
        warnings = validate_test_quality(f, project)
        info = [w for w in warnings if w.check == "discovery"]
        assert len(info) == 0

    def test_bad_naming_gives_info(self, project):
        f = project / "tests" / "checks.py"
        f.write_text("def test_x(): assert True")
        warnings = validate_test_quality(f, project)
        info = [w for w in warnings if w.check == "discovery"]
        assert len(info) >= 1


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------

class TestBoundaryConditions:
    def test_empty_file_has_no_parse_error(self, project):
        f = _write_test(project, "")
        warnings = validate_test_quality(f, project)
        parse_errors = [w for w in warnings if w.check == "parse"]
        assert len(parse_errors) == 0

    def test_syntax_error_reports_parse_error(self, project):
        f = _write_test(project, """
            def test_broken(
                assert True
        """)
        warnings = validate_test_quality(f, project)
        parse_errors = [w for w in warnings if w.severity == "error" and w.check == "parse"]
        assert len(parse_errors) == 1

    def test_non_python_content_reports_parse_error(self, project):
        f = _write_test(project, """
            <html>
              <body>not python</body>
            </html>
        """)
        warnings = validate_test_quality(f, project)
        parse_errors = [w for w in warnings if w.severity == "error" and w.check == "parse"]
        assert len(parse_errors) == 1


# ---------------------------------------------------------------------------
# Integration: full validation on realistic test file
# ---------------------------------------------------------------------------

class TestFullValidation:
    def test_realistic_good_test(self, project):
        """A realistic good test file should have zero errors."""
        (project / "tests" / "conftest.py").write_text(textwrap.dedent("""
            import subprocess, sys
            def run_app(*args, db=None):
                cmd = [sys.executable, "-m", "myapp"] + list(args)
                return subprocess.run(cmd, capture_output=True, text=True)
        """))
        f = _write_test(project, """
            from tests.conftest import run_app
            import subprocess, sys

            def test_add():
                r = run_app("add", "my task")
                assert r.returncode == 0
                assert "Added" in r.stdout

            def test_search():
                run_app("add", "find me")
                r = run_app("search", "find")
                assert r.returncode == 0

            def test_search_no_match():
                r = run_app("search", "nonexistent")
                assert r.returncode == 0
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error"]
        assert len(errors) == 0

    def test_realistic_bad_test(self, project):
        """A test with multiple issues should catch them all."""
        f = _write_test(project, """
            import pytest
            import subprocess, sys

            @pytest.mark.skip
            def test_skipped():
                assert False

            def test_tautology():
                assert True

            def test_wrong_app():
                subprocess.run([sys.executable, "-m", "badapp", "add", "x"])
        """)
        warnings = validate_test_quality(f, project)
        errors = [w for w in warnings if w.severity == "error"]
        # Should catch: skip, tautology, wrong module name
        assert len(errors) >= 3

    def test_non_pytest_framework_skips(self, project):
        """Non-pytest frameworks should return no warnings."""
        f = _write_test(project, """
            assert True
        """)
        warnings = validate_test_quality(f, project, framework="jest")
        assert len(warnings) == 0
