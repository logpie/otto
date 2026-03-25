"""Claim verification — regex audit of coding agent log against actual evidence.

No LLM needed. Checks that the agent's claims ("tests pass", "no errors")
match the actual exit codes and outputs captured during verification.
"""

import re
from pathlib import Path


def verify_claims(
    agent_log_path: Path,
    verify_log_path: Path | None = None,
) -> list[dict]:
    """Audit agent's claims against actual evidence.

    Scans the coding agent's log for claims about test results, build success,
    etc. Cross-references against actual verify log exit codes and outputs.

    Returns list of findings: [{claim, evidence, verified: bool, severity}]
    """
    findings: list[dict] = []

    if not agent_log_path.exists():
        return findings

    agent_text = agent_log_path.read_text()

    verify_text = ""
    if verify_log_path and verify_log_path.exists():
        verify_text = verify_log_path.read_text()

    # --- Pattern 1: Agent claims "all tests pass" ---
    test_pass_claims = re.findall(
        r"(?:all\s+\d+\s+tests?\s+pass|tests?\s+(?:all\s+)?pass(?:ed|ing)?|"
        r"\d+\s+(?:tests?\s+)?passed,?\s+0\s+failed)",
        agent_text,
        re.IGNORECASE,
    )
    if test_pass_claims and verify_text:
        # Check if verify log shows failure
        has_verify_fail = bool(re.search(r": FAIL", verify_text))
        has_test_fail = bool(re.search(
            r"(?:\d+\s+failed|FAIL(?:ED)?|AssertionError|Error:)",
            verify_text,
        ))
        if has_verify_fail or has_test_fail:
            findings.append({
                "claim": f"Agent claimed: '{test_pass_claims[0]}'",
                "evidence": "Verify log shows test failures",
                "verified": False,
                "severity": "hard",
            })
        else:
            findings.append({
                "claim": f"Agent claimed: '{test_pass_claims[0]}'",
                "evidence": "Verify log confirms pass",
                "verified": True,
                "severity": "info",
            })

    # --- Pattern 2: Agent claims "build succeeds" / "no errors" ---
    build_claims = re.findall(
        r"(?:build\s+succeed(?:s|ed)?|no\s+(?:build\s+)?errors?|"
        r"compil(?:es?|ation)\s+succeed(?:s|ed)?|tsc.*(?:pass|success|no\s+error))",
        agent_text,
        re.IGNORECASE,
    )
    if build_claims and verify_text:
        has_build_fail = bool(re.search(
            r"(?:error\s+TS\d+|SyntaxError|compilation\s+failed|build\s+failed)",
            verify_text,
            re.IGNORECASE,
        ))
        if has_build_fail:
            findings.append({
                "claim": f"Agent claimed: '{build_claims[0]}'",
                "evidence": "Verify log shows build errors",
                "verified": False,
                "severity": "hard",
            })

    # --- Pattern 3: Agent ran a test command with non-zero exit ---
    # Current runner logs usually contain only the tool-use summary line
    # ("● Bash ...") plus the agent's narration, not the command's stdout/stderr.
    # That means this pattern rarely fires in production today. Keep it anyway:
    # it still helps in tests and will become useful immediately if log capture
    # expands to include command output in the future.
    # Look for Bash tool calls with test commands followed by error output.
    bash_blocks = re.findall(
        r"● Bash\s+(.*?)(?=\n● |\Z)",
        agent_text,
        re.DOTALL,
    )
    for block in bash_blocks:
        first_line = block.strip().splitlines()[0] if block.strip() else ""
        is_test_cmd = any(kw in first_line.lower() for kw in
                         ("jest", "pytest", "vitest", "cargo test", "go test",
                          "npm test", "npx test"))
        if is_test_cmd:
            # Check if output contains failure indicators
            has_fail = bool(re.search(
                r"(?:FAIL|failed|Error|exit\s+code\s+[1-9])",
                block,
                re.IGNORECASE,
            ))
            # Check if agent continued without acknowledging
            if has_fail:
                # Did agent acknowledge? Look for fix/retry language in the
                # agent's narration AFTER the test output within this block,
                # plus the text immediately following.
                block_pos = agent_text.find(block)
                after_text = agent_text[block_pos + len(block):block_pos + len(block) + 500]
                # Narration = non-output lines in the block (agent's own words)
                # plus text after the block up to next tool call
                search_text = after_text
                # Also check block for agent narration (sentences, not test output)
                for bline in block.splitlines():
                    bs = bline.strip()
                    # Skip likely test output lines (FAIL, passed, Error:, indented)
                    if not bs or bs.startswith(("FAIL", "PASS", "Error", " ")):
                        continue
                    if any(c.isdigit() for c in bs[:5]) and ("passed" in bs or "failed" in bs):
                        continue
                    search_text += " " + bs
                acknowledged = bool(re.search(
                    r"(?:fix|broken|issue|bug|wrong|retry|let me|need to|"
                    r"the test failed|tests? fail|failing)",
                    search_text,
                    re.IGNORECASE,
                ))
                if not acknowledged:
                    findings.append({
                        "claim": f"Agent ran test: '{first_line[:60]}'",
                        "evidence": "Test output shows failures but agent did not address them",
                        "verified": False,
                        "severity": "advisory",
                    })

    return findings


def format_claim_findings(findings: list[dict]) -> str:
    """Format findings as a human-readable summary."""
    if not findings:
        return ""

    lines = ["# Claim Verification", ""]
    hard_fails = [f for f in findings if not f["verified"] and f["severity"] == "hard"]
    advisory = [f for f in findings if not f["verified"] and f["severity"] == "advisory"]
    confirmed = [f for f in findings if f["verified"]]

    if hard_fails:
        lines.append("## FAILURES (evidence contradicts claims)")
        for f in hard_fails:
            lines.append(f"- {f['claim']}")
            lines.append(f"  Evidence: {f['evidence']}")
        lines.append("")

    if advisory:
        lines.append("## Warnings (unaddressed issues)")
        for f in advisory:
            lines.append(f"- {f['claim']}")
            lines.append(f"  Evidence: {f['evidence']}")
        lines.append("")

    if confirmed:
        lines.append("## Confirmed")
        for f in confirmed:
            lines.append(f"- {f['claim']}")
        lines.append("")

    return "\n".join(lines)
