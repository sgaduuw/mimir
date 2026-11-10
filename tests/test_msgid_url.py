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
        # tip-tree synthetic tags: match no posted message, excluded on purpose
        "http://lkml.kernel.org/n/tip-abc123@git.kernel.org",
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


@pytest.mark.parametrize(
    "url",
    ["https://msgid.link/a@x", "https://msgid.link/r/a@x", "http://msgid.link/a@x/"],
)
def test_msgid_link_alias_names_the_msgid(url):
    """b4's alias of patch.msgid.link (#1142)."""
    assert msgid_from_url(url) == "a@x"


@pytest.mark.parametrize(
    "url",
    [
        "https://patchwork.freedesktop.org/patch/msgid/a@x",
        "http://patchwork.freedesktop.org/patch/msgid/a@x/",
    ],
)
def test_freedesktop_patchwork_msgid_path_names_the_msgid(url):
    assert msgid_from_url(url) == "a@x"


@pytest.mark.parametrize(
    "url",
    [
        "https://patchwork.freedesktop.org/patch/123456/",
        "https://patchwork.freedesktop.org/patch/123456@x/",
        "https://patchwork.freedesktop.org/series/123456/",
        "https://patchwork.freedesktop.org/patch/msgid",
        "https://patchwork.freedesktop.org/patch/msgid/",
        "https://patchwork.freedesktop.org/patch/msgids/a@x",
        "https://patchwork.freedesktop.org/series/msgid/a@x",
    ],
)
def test_freedesktop_patchwork_other_paths_are_not_msgids(url):
    assert msgid_from_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://lore.kernel.org/r/a%40x",
        "https://lore.kernel.org/all/a%40x/T/",
        "http://lkml.kernel.org/r/a%40x",
        "https://patch.msgid.link/a%40x",
        "https://msgid.link/a%40x",
        "https://patchwork.freedesktop.org/patch/msgid/a%40x",
    ],
)
def test_percent_encoded_at_is_unquoted(url):
    """Mainline trailers carry `%40` for `@` on every accepted host (#1142)."""
    assert msgid_from_url(url) == "a@x"


def test_percent_encoded_slash_stays_inside_the_msgid():
    assert msgid_from_url("https://lore.kernel.org/r/a%2Fb%40x") == "a/b@x"


@pytest.mark.parametrize(
    "url",
    [
        "https://lore.kernel.org/r/a%2Fb",
        "https://lore.kernel.org/r/a%2540x",
        "http://lkml.kernel.org/lkml/a%40x",
        "https://patchwork.freedesktop.org/patch/123%40x/",
        "http://lkml.kernel.org/%72/a@x",
    ],
)
def test_encoded_oddities_without_an_at_are_not_msgids(url):
    """`%2F` alone makes no msgid, `%2540` unquotes once to `a%40x` (no
    `@`), and the host's path prefix is still read from the raw path."""
    assert msgid_from_url(url) is None
