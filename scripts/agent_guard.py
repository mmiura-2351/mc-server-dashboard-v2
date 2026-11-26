#!/usr/bin/env python3
"""Pre-command hook for coding agents (root AGENTS.md Section 5.6).

Claude Code (.claude/settings.json) and Codex (.codex/hooks.json) both run this
before a shell command and pass the same JSON on stdin:
``{"tool_name": "Bash", "tool_input": {"command": "..."}, ...}``. Claude Code
adds ``agent_id`` when the caller is a subagent; Codex does not say.

It refuses two things:

1. Adding the ``approved`` label with ``gh``. Only the owner applies it.
2. Creating an issue from a subagent. A subagent reports, and the lead files.

Exit status 2 with the reason on stderr refuses the command in both harnesses.
Every other outcome exits 0, including input this script cannot read: it runs
before every shell command, so a bug here must never stop unrelated work.

This is a guard against mistakes, not a security boundary. ``sh -c '...'`` or a
JSON body passed with ``--input`` goes past it.

Run ``scripts/agent_guard.py --self-test`` to check the rules against fixed
cases. Pure standard library.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

APPROVED = "approved"
LABEL_FLAGS = ("--label", "-l", "--add-label")
FIELD_FLAGS = ("-f", "-F", "--field", "--raw-field")
# ``gh api`` flags that take a value, so the value is not read as the endpoint.
API_VALUE_FLAGS = FIELD_FLAGS + (
    "-X",
    "--method",
    "-H",
    "--header",
    "--input",
    "-q",
    "--jq",
    "-t",
    "--template",
    "--cache",
    "--hostname",
    "-p",
    "--preview",
)
OPERATORS = {";", "&&", "||", "|", "&", "(", ")"}

LABEL_REASON = (
    "Refused: only the owner applies the 'approved' label (AGENTS.md Section 5.1)."
)
SUBAGENT_REASON = (
    "Refused: a subagent does not file issues. Report the defect to the lead "
    "(AGENTS.md Section 5.3)."
)

# A here-document body is text, not commands: ``cat > body.md <<EOF ... EOF``.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2\s*$", re.DOTALL | re.MULTILINE)

# Used only when the command cannot be tokenised (unbalanced quotes).
FALLBACK_LABEL = re.compile(
    r"\bgh\s+(?:issue|pr)\s+(?:create|new|edit)\b[^\n;|&]*?"
    r"(?:--add-label|--label|-l)[=\s]+[\"']?[\w,-]*\bapproved\b",
    re.IGNORECASE,
)


def one_line(command: str) -> str:
    """Rewrite ``command`` so that a line break outside quotes reads as ``;``.

    Outside quotes it also drops a backslash-newline and a ``#`` comment, as
    the shell does. Text inside quotes is copied unchanged.
    """
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        following = command[index + 1 : index + 2]
        if quote:
            out.append(char)
            if char == "\\" and quote == '"' and following:
                out.append(following)
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char == "\\" and following:
            if following != "\n":
                out.append(char + following)
            index += 1
        elif char == "#" and (not out or out[-1][-1] in " \t;|&("):
            while index < len(command) and command[index] != "\n":
                index += 1
            continue
        elif char == "\n":
            out.append(" ; ")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def gh_invocations(command: str) -> list[list[str]]:
    """Return the argument list of every ``gh`` call in ``command``.

    Each list ends at the next shell operator, line break, or ``gh``.
    """
    command = one_line(HEREDOC.sub(" ", command))
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    calls: list[list[str]] = []
    current: list[str] | None = None
    for token in tokens:
        if token == "gh" or token.endswith("/gh"):
            current = []
            calls.append(current)
        elif token in OPERATORS:
            current = None
        elif current is not None:
            current.append(token)
    return calls


def flag_values(args: list[str], flags: tuple[str, ...]) -> list[str]:
    """Values given to any of ``flags``, as ``--flag value`` or ``--flag=value``."""
    values = []
    for index, arg in enumerate(args):
        if arg in flags and index + 1 < len(args):
            values.append(args[index + 1])
        else:
            for flag in flags:
                if flag.startswith("--") and arg.startswith(flag + "="):
                    values.append(arg[len(flag) + 1 :])
    return values


def adds_approved(args: list[str]) -> bool:
    words = [arg for arg in args if not arg.startswith("-")]
    if words[:1] and words[0] in ("issue", "pr") and words[1:2]:
        if words[1] in ("create", "new", "edit"):
            for value in flag_values(args, LABEL_FLAGS):
                if APPROVED in (label.strip().lower() for label in value.split(",")):
                    return True
    if words[:1] == ["api"]:
        for value in flag_values(args, FIELD_FLAGS):
            key, _, label = value.partition("=")
            if key.startswith("labels") and label.strip().lower() == APPROVED:
                return True
    return False


def api_endpoint(args: list[str]) -> str:
    """The endpoint of a ``gh api`` call: its first argument after ``api`` that
    is neither a flag nor the value of one."""
    is_value = False
    for arg in args[args.index("api") + 1 :]:
        if is_value:
            is_value = False
        elif arg.startswith("-"):
            is_value = arg in API_VALUE_FLAGS
        else:
            return arg
    return ""


def creates_issue(args: list[str]) -> bool:
    words = [arg for arg in args if not arg.startswith("-")]
    if words[:2] in (["issue", "create"], ["issue", "new"]):
        return True
    if words[:1] == ["api"]:
        endpoint = api_endpoint(args).split("?")[0].rstrip("/")
        writes = bool(flag_values(args, FIELD_FLAGS)) or any(
            method.upper() == "POST" for method in flag_values(args, ("-X", "--method"))
        )
        return endpoint.endswith("/issues") and writes
    return False


def decide(command: str, is_subagent: bool) -> str | None:
    """Return the reason to refuse ``command``, or None to let it run."""
    try:
        calls = gh_invocations(command)
    except ValueError:
        text = HEREDOC.sub(" ", command)
        return LABEL_REASON if FALLBACK_LABEL.search(text) else None
    for args in calls:
        if adds_approved(args):
            return LABEL_REASON
        if is_subagent and creates_issue(args):
            return SUBAGENT_REASON
    return None


def reason_for(raw: str) -> str | None:
    """Return the reason to refuse the hook payload ``raw``, or None."""
    try:
        payload = json.loads(raw)
        if payload.get("tool_name") != "Bash":
            return None
        command = payload["tool_input"]["command"]
        return decide(command, is_subagent=bool(payload.get("agent_id")))
    except Exception:  # noqa: BLE001 -- never block unrelated work on a bug here
        return None


def main() -> int:
    reason = reason_for(sys.stdin.read())
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


# (command, is_subagent, refused?)
CASES = [
    ("gh issue edit 12 --add-label approved", False, True),
    ("gh issue edit 12 --add-label=approved", False, True),
    ("gh issue create --title t --label bug,approved --body b", False, True),
    ("gh pr edit 7 --add-label Approved", False, True),
    ('gh api -X POST repos/o/r/issues/12/labels -f "labels[]=approved"', False, True),
    ("cd /tmp && GH_PAGER= /usr/bin/gh issue edit 12 -l approved", False, True),
    ("gh issue edit 12 --add-label 'approved", False, True),
    ("gh issue create --title t --label bug,low-priority", True, True),
    ("gh api repos/o/r/issues -f title=t -f body=b", True, True),
    ("gh issue list --state open --label approved --json number,title", False, False),
    ("gh issue list --label approved", True, False),
    ("gh issue create --title t --label bug,low-priority --body b", False, False),
    ("gh issue edit 12 --remove-label approved", False, False),
    ("gh label create approved --description d", False, False),
    ('gh pr create --title "Add the approved label rule" --label chore', False, False),
    (
        'gh pr review 9 --comment --body "VERDICT: APPROVE --add-label approved"',
        True,
        False,
    ),
    (
        "gh issue edit 12 --add-label low-priority; gh issue list --label approved",
        False,
        False,
    ),
    ("gh api repos/o/r/issues?labels=approved", True, False),
    ("gh issue comment 12 --body 'not approved yet'", True, False),
    ("make api-test", True, False),
    ("echo it's fine", False, False),
    (
        "cat > body.md <<'EOF'\nRun gh issue edit 3 --add-label approved\nEOF",
        False,
        False,
    ),
    (
        "cat > body.md <<EOF\n"
        "It's the owner's call: gh issue edit 3 --add-label approved\nEOF",
        False,
        False,
    ),
    (
        "cat > b.md <<EOF\ntext\nEOF\ngh issue edit 3 --add-label approved",
        False,
        True,
    ),
    # A line break ends a command.
    ("gh pr edit 7 --add-label chore\ngrep -l approved docs/dev/*.md", False, False),
    ("gh pr edit 7 --add-label chore\n\ngrep -l approved f", False, False),
    ("gh pr edit 7 --add-label chore # note\ngrep -l approved f", False, False),
    ("gh pr edit 7 --add-label chore &&\ngrep -l approved f", False, False),
    ("make docs-check\ngh issue edit 3 --add-label approved", False, True),
    ("gh issue edit 3 \\\n  --add-label approved", False, True),
    ('gh issue edit 3 --body "one\ntwo" --add-label approved', False, True),
    ('gh issue edit 3 --title "#3 again" --add-label approved', False, True),
    ("gh issue view o/r#5; gh issue edit 3 --add-label approved", False, True),
    # ``gh api``: flags may come before the endpoint.
    ("gh api -X POST repos/o/r/issues -f title=t", True, True),
    ("gh api --method POST repos/o/r/issues -f title=t", True, True),
    ('gh api --method POST -H "Accept: x" /repos/o/r/issues -f title=t', True, True),
    ("gh api -X POST repos/o/r/issues -f title=t", False, False),
    ("gh api -X GET repos/o/r/issues", True, False),
    (
        'gh api repos/o/r/issues/5/comments -f body="see github.com/o/r/issues"',
        True,
        False,
    ),
]

# (hook payload, refused?) -- input the script cannot read is always allowed.
PAYLOADS = [
    ("", False),
    ("not json", False),
    ("[]", False),
    ('{"tool_name": "Read", "tool_input": {"file_path": "approved"}}', False),
    ('{"tool_name": "Bash", "tool_input": {}}', False),
    ('{"tool_name": "Bash", "tool_input": {"command": "gh issue create -t x"}}', False),
    (
        '{"tool_name": "Bash", "agent_id": "a1", '
        '"tool_input": {"command": "gh issue create -t x"}}',
        True,
    ),
    (
        '{"tool_name": "Bash", "tool_input": '
        '{"command": "gh issue edit 3 --add-label approved"}}',
        True,
    ),
]


def self_test() -> int:
    failures = [
        f"{command!r} (subagent={is_subagent}): expected "
        f"{'refused' if refused else 'allowed'}"
        for command, is_subagent, refused in CASES
        if (decide(command, is_subagent) is not None) != refused
    ]
    failures += [
        f"payload {raw!r}: expected {'refused' if refused else 'allowed'}"
        for raw, refused in PAYLOADS
        if (reason_for(raw) is not None) != refused
    ]
    for failure in failures:
        print(f"agent_guard self-test FAILED: {failure}", file=sys.stderr)
    if not failures:
        print(f"agent_guard self-test OK ({len(CASES) + len(PAYLOADS)} cases)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(self_test() if sys.argv[1:] == ["--self-test"] else main())
