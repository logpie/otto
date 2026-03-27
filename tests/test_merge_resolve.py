import subprocess
from unittest.mock import patch

from otto import merge_resolve


def test_resolve_one_uses_tool_free_cli_and_three_way_prompt(tmp_path):
    conflicted = tmp_path / "conflicted.txt"
    conflicted.write_text("<<<<<<< ours\nfoo\n=======\nbar\n>>>>>>> theirs\n")
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "diff"] and "--numstat" in cmd:
            # _is_text_file check — report as text file
            return subprocess.CompletedProcess(cmd, 0, "1\t0\tconflicted.txt\n", "")
        if cmd[:2] == ["git", "show"]:
            stage_to_content = {
                ":1:conflicted.txt": "base line\n",
                ":2:conflicted.txt": "ours line\n",
                ":3:conflicted.txt": "theirs line\n",
            }
            return subprocess.CompletedProcess(cmd, 0, stage_to_content[cmd[2]], "")
        if cmd[0] == "claude":
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, "resolved line\n", "")
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("otto.merge_resolve.subprocess.run", side_effect=fake_run):
        resolved = merge_resolve._resolve_one(tmp_path, "conflicted.txt")

    assert resolved == "resolved line"
    cmd = seen["cmd"]
    assert cmd[:5] == ["claude", "--print", "--model", "haiku", "--permission-mode"]
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--bare" in cmd
    assert "-" in cmd  # stdin mode
    # Prompt is now passed via stdin (input kwarg), not as last argv element
    kwargs = seen["kwargs"]
    prompt = kwargs.get("input", "")
    assert "Current conflicted worktree file" in prompt
    assert "Base version (stage 1)" in prompt
    assert "Ours version (stage 2)" in prompt
    assert "Theirs version (stage 3)" in prompt
    assert "base line" in prompt
    assert "ours line" in prompt
    assert "theirs line" in prompt
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 120
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True


def test_resolve_conflicts_with_llm_keeps_repo_unchanged_on_partial_failure(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first original\n")
    second.write_text("second original\n")

    with patch(
        "otto.merge_resolve._resolve_one",
        side_effect=["first resolved\n", None],
    ), patch("otto.merge_resolve.subprocess.run") as run_mock:
        success = merge_resolve.resolve_conflicts_with_llm(
            tmp_path,
            ["first.txt", "second.txt"],
        )

    assert not success
    assert first.read_text() == "first original\n"
    assert second.read_text() == "second original\n"
    run_mock.assert_not_called()
