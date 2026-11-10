"""`msgid_from_url`: which hosts and paths name a Message-ID (#1142)."""

import pytest

from mimir.msgid_url import msgid_from_url


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_lkml_kernel_org_r_path_names_the_msgid(scheme):
    assert msgid_from_url(f"{scheme}://lkml.kernel.org/r/a.b@example.com") == (
        "a.b@example.com"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://lkml.kernel.org/lkml/2011/1/2/3",
        "http://lkml.kernel.org/lkml/2011/1/2/3@x",
        "http://lkml.kernel.org/x/r/a@example.com",
        "http://lkml.kernel.org/rr/a@example.com",
        "http://lkml.kernel.org/r/",
        "http://lkml.kernel.org/",
    ],
)
def test_lkml_kernel_org_other_paths_are_not_msgids(url):
    assert msgid_from_url(url) is None


def test_lore_shapes_still_resolve():
    assert msgid_from_url("https://lore.kernel.org/all/a@x/T/") == "a@x"
    assert msgid_from_url("https://patch.msgid.link/a@x") == "a@x"
