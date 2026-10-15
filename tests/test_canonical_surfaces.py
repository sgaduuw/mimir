"""#675 / #646 class: every surface that reads `articles.canonical_inbox_id`
must name the inbox `fallback_canonical_name` names, including when the
stored value is NULL or names an inbox the article is not linked to."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from mimir.canonical import fallback_canonical_name
from mimir.models import Article, ArticleFile, ArticleList, Inbox
from mimir.subsystems import recent_patches_touching
from mimir.subsystems_dashboard import recent_articles_in_subsystem
from mimir.web.urls import _canonical_inbox_name, _canonical_inbox_names_for
from tests.test_subsystems._helpers import _add_subsystem


@pytest.mark.parametrize("stored", ["member", "non_member", "none"])
def test_every_surface_agrees_with_fallback_canonical_name(seeded_db, stored):
    with seeded_db() as s:
        alpha = s.execute(select(Inbox).where(Inbox.name == "alpha")).scalar_one()
        beta = s.execute(select(Inbox).where(Inbox.name == "beta")).scalar_one()
        gamma = Inbox(name="gamma", mirror_path="/x", upstream_url="https://x/g")
        s.add(gamma)
        s.flush()
        # beta sorts after alpha, so a stored beta differs from the
        # alphabetical fallback and the member case is not vacuous.
        pin = {"member": beta.id, "non_member": gamma.id, "none": None}[stored]
        sub = _add_subsystem(s, "AGREE", "Supported", files=["fs/agree/"])
        art = Article(
            message_id="agree@x",
            subject="s",
            author="a@example",
            date=datetime.now(UTC),
            thread_parent=None,
            subject_normalized="s",
            canonical_inbox_id=pin,
            lists=[
                ArticleList(inbox_id=alpha.id, epoch="0.git", commit_sha="a" * 40),
                ArticleList(inbox_id=beta.id, epoch="0.git", commit_sha="b" * 40),
            ],
            files=[ArticleFile(path="fs/agree/x.c")],
        )
        s.add(art)
        s.commit()

        links = [(alpha.id, "alpha"), (beta.id, "beta")]
        expected = fallback_canonical_name(pin, links)
        assert expected == ("beta" if stored == "member" else "alpha")

        assert _canonical_inbox_name(art, links) == expected
        assert _canonical_inbox_names_for(s, [art.id]) == {art.id: expected}
        dash = recent_articles_in_subsystem(s, alpha, sub, force=True)
        assert [(p.message_id, p.inbox_name) for p in dash] == [("agree@x", expected)]
        related = recent_patches_touching(s, ["fs/agree/x.c"])
        assert [(p.message_id, p.inbox_name) for p in related] == [
            ("agree@x", expected)
        ]
