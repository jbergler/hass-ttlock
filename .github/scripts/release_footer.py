"""Add the contributor thanks and support line to a release's notes.

Reads the release body on stdin and writes the new body to stdout. The block
goes just before GitHub's "**Full Changelog**" line, so it sits right under the
list of changes, which is what HACS shows in its update dialog. Running it twice
replaces the block instead of adding a second one. A footer pasted by hand in
the old format is removed.
"""

from pathlib import Path
import re
import sys

FOOTER_TEMPLATE = Path(__file__).parent.parent / "release-footer.md"
MAINTAINER = "jbergler"
NOT_THANKED = {MAINTAINER, "copilot", "dependabot", "renovate", "github-actions"}
START = "<!-- support:start -->"
END = "<!-- support:end -->"

_BLOCK = re.compile(rf"\n*{re.escape(START)}.*?{re.escape(END)}\n*", re.DOTALL)
_LEGACY = re.compile(
    r"\n*If you found this helpful and you'd like to say thanks.*?buymeacoffee\.com/jbergler[^\n]*\n*",
    re.DOTALL,
)
_AUTHOR = re.compile(r" by @([A-Za-z0-9-]+(?:\[bot\])?) in https://")
_FULL_CHANGELOG = re.compile(r"^\*\*Full Changelog\*\*", re.MULTILINE)


def contributors(body: str) -> list[str]:
    """Return the people to thank by name, in order of appearance."""
    seen: dict[str, None] = {}
    for login in _AUTHOR.findall(body):
        if login.endswith("[bot]") or login.lower() in NOT_THANKED:
            continue
        seen.setdefault(login, None)
    return list(seen)


def thanks_line(logins: list[str]) -> str:
    """Return a sentence thanking the release's outside contributors."""
    names = [f"@{login}" for login in logins]
    if len(names) == 1:
        joined = names[0]
    elif len(names) == 2:
        joined = f"{names[0]} and {names[1]}"
    else:
        joined = f"{', '.join(names[:-1])}, and {names[-1]}"
    return f"Thanks to {joined} for contributing to this release."


def apply_footer(body: str, footer: str) -> str:
    """Return the body with exactly one support block in it."""
    body = body.replace("\r\n", "\n")
    body = _BLOCK.sub("\n\n", body)
    body = _LEGACY.sub("\n\n", body).strip()

    parts = []
    if logins := contributors(body):
        parts.append(thanks_line(logins))
    parts.append(footer.strip())
    block = "\n".join([START, "\n\n".join(parts), END])

    if match := _FULL_CHANGELOG.search(body):
        before = body[: match.start()].rstrip()
        return f"{before}\n\n{block}\n\n{body[match.start() :]}\n"
    return f"{body}\n\n{block}\n"


def main() -> None:
    """Rewrite the body read on stdin."""
    sys.stdout.write(apply_footer(sys.stdin.read(), FOOTER_TEMPLATE.read_text()))


if __name__ == "__main__":
    main()
