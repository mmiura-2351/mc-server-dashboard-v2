#!/usr/bin/env python3
"""Hook for coding agents (root AGENTS.md Section 5.6).

Claude Code (.claude/settings.json) and Codex (.codex/hooks.json) both run this
on two events and pass JSON on stdin.

Before a shell command (``{"tool_name": "Bash", "tool_input": {"command":
"..."}, "session_id": "..."}``) it refuses two things:

1. Adding the ``approved`` label to an issue the owner did not name. The one
   form it lets through is ``gh issue edit <numbers> --add-label approved``,
   from the main session, when the owner typed every number in this session.
2. Creating an issue from a subagent. A subagent reports, and the lead files.
   Claude Code adds ``agent_id`` when the caller is a subagent; Codex does not
   say.

When the owner submits a prompt (``{"hook_event_name": "UserPromptSubmit",
"prompt": "...", "session_id": "..."}``) it records the numbers in the prompt,
which is how rule 1 knows what the owner named. The record is a file outside
the repository, one per session. This event never refuses and prints nothing.

Exit status 2 with the reason on stderr refuses the command in both harnesses.
Every other outcome exits 0, including input this script cannot read: it runs
before every shell command, so a bug here must never stop unrelated work. The
one thing that fails the other way is the record: when it is missing or cannot
be read, adding ``approved`` is refused.

This is a guard against mistakes, not a security boundary. ``sh -c '...'``, a
JSON body passed with ``--input``, or writing the record by hand goes past it.

Run ``scripts/agent_guard.py --self-test`` to check the rules against fixed
cases. Pure standard library.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
import unicodedata
from pathlib import Path

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

# Where the numbers the owner typed are kept, one file per session.
STATE_DIR = Path(tempfile.gettempdir()) / f"agent-guard-{os.getuid()}"

LABEL_FORM_REASON = (
    "Refused: add the 'approved' label only as "
    "`gh issue edit <numbers> --add-label approved`, and only to issues the "
    "owner named (AGENTS.md Section 5.1)."
)
LABEL_UNNAMED_REASON = (
    "Refused: the owner has not named {} in this session. Add the 'approved' "
    "label only to issues the owner named (AGENTS.md Section 5.1)."
)
LABEL_SUBAGENT_REASON = (
    "Refused: a subagent never adds the 'approved' label (AGENTS.md Section 5.1)."
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


def state_path(session_id: object) -> Path | None:
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))
    return STATE_DIR / name if name else None


def record_prompt(payload: dict) -> None:
    """Remember every number in a prompt the owner typed.

    A prompt that starts with ``<`` is a notice from the harness, such as a
    finished background task. It is not the owner's words, so it is skipped.
    """
    prompt = unicodedata.normalize("NFKC", str(payload.get("prompt") or ""))
    path = state_path(payload.get("session_id"))
    numbers = [str(int(number)) for number in re.findall(r"\d+", prompt)]
    if path is None or not numbers or prompt.lstrip().startswith("<"):
        return
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    with path.open("a") as record:
        record.write("\n".join(numbers) + "\n")


def named_numbers(session_id: object) -> frozenset[str]:
    """The numbers the owner typed in this session. Empty when unknown."""
    path = state_path(session_id)
    try:
        return frozenset(path.read_text().split()) if path else frozenset()
    except OSError:
        return frozenset()


def approved_targets(args: list[str]) -> list[str] | None:
    """Issue numbers of ``issue edit <numbers> --add-label approved``.

    None when the call has any other shape: another flag, a URL, no number.
    """
    if args[:2] != ["issue", "edit"]:
        return None
    rest = [arg.lower() for arg in args[2:]]
    for pair in (["--add-label", APPROVED], ["--add-label=" + APPROVED]):
        for start in range(len(rest) - len(pair) + 1):
            if rest[start : start + len(pair)] == pair:
                numbers = rest[:start] + rest[start + len(pair) :]
                if numbers and all(n.isascii() and n.isdigit() for n in numbers):
                    return [str(int(number)) for number in numbers]
    return None


def label_refusal(
    args: list[str], is_subagent: bool, named: frozenset[str]
) -> str | None:
    """Reason to refuse a call that adds ``approved``, or None to let it run."""
    if is_subagent:
        return LABEL_SUBAGENT_REASON
    numbers = approved_targets(args)
    if numbers is None:
        return LABEL_FORM_REASON
    unnamed = ["#" + number for number in numbers if number not in named]
    if unnamed:
        return LABEL_UNNAMED_REASON.format(", ".join(unnamed))
    return None


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


def decide(
    command: str, is_subagent: bool, named: frozenset[str] = frozenset()
) -> str | None:
    """Return the reason to refuse ``command``, or None to let it run.

    ``named`` holds the numbers the owner typed in this session.
    """
    try:
        calls = gh_invocations(command)
    except ValueError:
        text = HEREDOC.sub(" ", command)
        return LABEL_FORM_REASON if FALLBACK_LABEL.search(text) else None
    for args in calls:
        if adds_approved(args):
            reason = label_refusal(args, is_subagent, named)
            if reason:
                return reason
        if is_subagent and creates_issue(args):
            return SUBAGENT_REASON
    return None


def reason_for(raw: str) -> str | None:
    """Return the reason to refuse the hook payload ``raw``, or None."""
    try:
        payload = json.loads(raw)
        if payload.get("hook_event_name") == "UserPromptSubmit":
            record_prompt(payload)
            return None
        if payload.get("tool_name") != "Bash":
            return None
        command = payload["tool_input"]["command"]
        named = (
            named_numbers(payload.get("session_id"))
            if APPROVED in command.lower()
            else frozenset()
        )
        return decide(command, bool(payload.get("agent_id")), named)
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

# (command, is_subagent, numbers the owner named, refused?)
NAMED_CASES = [
    ("gh issue edit 12 --add-label approved", False, {"12"}, False),
    ("gh issue edit 12 15 --add-label approved", False, {"12", "15"}, False),
    ("gh issue edit 12 --add-label=Approved", False, {"12"}, False),
    ("gh issue edit --add-label approved 12", False, {"12"}, False),
    ("gh issue edit 12 15 --add-label approved", False, {"12"}, True),
    ("gh issue edit 12 --add-label approved", True, {"12"}, True),
    ("gh issue edit 12 --add-label approved --repo o/r", False, {"12"}, True),
    ("gh issue edit 12 --add-label approved,bug", False, {"12"}, True),
    (
        "gh issue edit https://github.com/o/r/issues/12 --add-label approved",
        False,
        {"12"},
        True,
    ),
    ("gh issue create --title t --label approved", False, {"12"}, True),
    ("gh pr edit 12 --add-label approved", False, {"12"}, True),
    (
        'gh api -X POST repos/o/r/issues/12/labels -f "labels[]=approved"',
        False,
        {"12"},
        True,
    ),
]

LABEL_12 = {
    "tool_name": "Bash",
    "tool_input": {"command": "gh issue edit 12 4101 --add-label approved"},
}
LABEL_77 = {
    "tool_name": "Bash",
    "tool_input": {"command": "gh issue edit 77 --add-label approved"},
}

# (hook payload, refused?), run in order against one empty record directory.
FLOW = [
    ({**LABEL_12, "session_id": "s1"}, True),
    (
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s1",
            "prompt": "approve 12 and \uff14\uff11\uff10\uff11",
        },
        False,
    ),
    ({**LABEL_12, "session_id": "s1"}, False),
    ({**LABEL_12, "session_id": "s2"}, True),
    ({**LABEL_12, "session_id": "s1", "agent_id": "a1"}, True),
    ({**LABEL_12}, True),
    (
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s1",
            "prompt": "<task-notification>\nfiled #77\n</task-notification>",
        },
        False,
    ),
    ({**LABEL_77, "session_id": "s1"}, True),
    (
        {"hook_event_name": "UserPromptSubmit", "session_id": "../s3", "prompt": "77"},
        False,
    ),
    ({**LABEL_77, "session_id": "s3"}, False),
    ({"hook_event_name": "UserPromptSubmit", "prompt": "77"}, False),
    ({"hook_event_name": "UserPromptSubmit", "session_id": "s4"}, False),
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
    global STATE_DIR
    failures = [
        f"{command!r} (subagent={is_subagent}): expected "
        f"{'refused' if refused else 'allowed'}"
        for command, is_subagent, refused in CASES
        if (decide(command, is_subagent) is not None) != refused
    ]
    failures += [
        f"{command!r} (subagent={is_subagent}, named={sorted(named)}): expected "
        f"{'refused' if refused else 'allowed'}"
        for command, is_subagent, named, refused in NAMED_CASES
        if (decide(command, is_subagent, frozenset(named)) is not None) != refused
    ]
    with tempfile.TemporaryDirectory() as scratch:
        real_state_dir, STATE_DIR = STATE_DIR, Path(scratch) / "state"
        try:
            for step, (payload, refused) in enumerate(FLOW, start=1):
                if (reason_for(json.dumps(payload)) is not None) != refused:
                    failures.append(
                        f"flow step {step}: expected "
                        f"{'refused' if refused else 'allowed'}"
                    )
            if [path.name for path in STATE_DIR.iterdir() if path.name != "s1"] != [
                "s3"
            ]:
                failures.append("flow: unexpected record files")
        finally:
            STATE_DIR = real_state_dir
    failures += [
        f"payload {raw!r}: expected {'refused' if refused else 'allowed'}"
        for raw, refused in PAYLOADS
        if (reason_for(raw) is not None) != refused
    ]
    for failure in failures:
        print(f"agent_guard self-test FAILED: {failure}", file=sys.stderr)
    if not failures:
        total = len(CASES) + len(NAMED_CASES) + len(FLOW) + len(PAYLOADS)
        print(f"agent_guard self-test OK ({total} cases)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(self_test() if sys.argv[1:] == ["--self-test"] else main())
