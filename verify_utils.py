import os
import sys
from pathlib import Path


def _detect_verification(project_dir: Path) -> dict[str, str | None]:
    """Detect how a project should be verified.

    Returns:
        {"type": "verify.sh" | "auto-tests" | "none",
         "detail": str,
         "cmd": str | None}
    """
    verify_script = project_dir / "verify.sh"
    non_exec_detail = ""
    if verify_script.exists():
        if os.access(verify_script, os.X_OK):
            try:
                detail = verify_script.read_text()[:500]
            except OSError:
                detail = "Executable verify.sh found."
            return {
                "type": "verify.sh",
                "detail": detail,
                "cmd": "bash verify.sh",
            }
        non_exec_detail = "verify.sh exists but is not executable. "

    test_files = sorted(
        {
            path.relative_to(project_dir).as_posix()
            for pattern in ("test_*.py", "*_test.py", "*/test_*.py", "*/*_test.py")
            for path in project_dir.glob(pattern)
        }
    )
    if test_files:
        return {
            "type": "auto-tests",
            "detail": non_exec_detail + ", ".join(test_files[:10]),
            "cmd": f"{sys.executable} -m pytest -x --tb=short",
        }

    return {
        "type": "none",
        "detail": non_exec_detail + "No verify.sh or test files found.",
        "cmd": None,
    }
