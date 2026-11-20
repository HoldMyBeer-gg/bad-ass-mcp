"""The sponsor badge must click through to the Sponsors page.

GitHub proxies README images through camo, which drops shields.io's
``link=`` parameter, so the badge has to be wrapped in a markdown link.
"""

import re
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"


def test_sponsor_badge_is_wrapped_in_a_link():
    pattern = (
        r"\[!\[[^\]]*\]\(https://img\.shields\.io/github/sponsors/jabberwock[^)]*\)\]"
        r"\(https://github\.com/sponsors/jabberwock\)"
    )
    assert re.search(pattern, README.read_text(encoding="utf-8"))


def test_every_shields_badge_is_clickable():
    text = README.read_text(encoding="utf-8")
    bare = re.findall(r"(?<!\[)!\[[^\]]*\]\(https://img\.shields\.io[^)]*\)", text)
    assert not bare, f"badges with no link wrapper: {bare}"
