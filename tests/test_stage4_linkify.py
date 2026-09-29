"""Stage 4 turns the bare URLs in a section into links.

Done in code, not in the prompt, and these tests are why: an href sits inside a tag, and
`content_stream` strips tags before every gate in ai_postprocess sees them, so a wrong
character in a href is the one corruption the stage-4 invariant cannot catch. Built from
the cell's own text, the destination cannot drift from what the page printed — which is
the property `test_visible_text_is_never_touched` pins down.
"""

from aosphere_core_index.extract.ai_postprocess import (
    content_stream,
    linkify_urls,
    strict_stream,
)


def test_a_bare_url_becomes_a_link():
    out, n = linkify_urls("<td>found here: https://www.scb.gov.bs/x.pdf</td>")
    assert n == 1
    assert '<a href="https://www.scb.gov.bs/x.pdf"' in out
    assert ">https://www.scb.gov.bs/x.pdf</a>" in out


def test_trailing_punctuation_stays_in_the_sentence():
    # "(https://x/a.pdf)." — the ")." belongs to the prose, never to the href, and it must
    # not be dropped from the text either.
    out, _ = linkify_urls("<td>(see https://x.gov/a.pdf).</td>")
    assert '<a href="https://x.gov/a.pdf"' in out
    assert out.endswith("</a>).</td>")


def test_a_url_the_pdf_wrapped_is_rejoined_in_the_href_only():
    # The page prints ".../IFA-Fund-Manager-Registration-IFR-Form-B.pdf"; the text layer
    # returns the line wrap as a space. The link has to work, and the cell has to still
    # read exactly as extracted.
    cell = ("<td>https://www.scb.gov.bs/wp-content/uploads/2020/05/IFA- "
            "Fund-Manager-Registration-IFR-Form-B.pdf</td>")
    out, n = linkify_urls(cell)
    assert n == 1
    assert ('href="https://www.scb.gov.bs/wp-content/uploads/2020/05/'
            'IFA-Fund-Manager-Registration-IFR-Form-B.pdf"') in out
    assert ">https://www.scb.gov.bs/wp-content/uploads/2020/05/IFA- Fund-" in out


def test_a_hyphen_in_prose_is_not_a_wrapped_url():
    out, _ = linkify_urls("<td>see https://x.gov/a.pdf - and then some words</td>")
    assert 'href="https://x.gov/a.pdf"' in out
    assert "andthen" not in out


def test_an_anchor_the_model_wrote_is_left_alone():
    # Stage 4 emits its own <a> now and then, unprompted. Wrapping it again would nest
    # anchors and put the URL on screen twice.
    cell = '<td><a href="https://scb.gov.bs/a.pdf">https://scb.gov.bs/a.pdf</a></td>'
    assert linkify_urls(cell) == (cell, 0)


def test_markdown_links_and_code_spans_are_left_alone():
    text = "[the form](https://x.gov/a.pdf) and `https://x.gov/b.pdf`"
    assert linkify_urls(text) == (text, 0)


def test_it_is_idempotent():
    once, n1 = linkify_urls("<td>a https://x.gov/a.pdf b https://y.gov/b.pdf</td>")
    twice, n2 = linkify_urls(once)
    assert n1 == 2
    assert (twice, n2) == (once, 0)


def test_visible_text_is_never_touched():
    # The whole point: linking is a rendering change, so both stage-4 streams — the one
    # that ignores punctuation and the one that does not — must come out identical.
    before = ("<td>(see https://www.scb.gov.bs/wp-content/uploads/2020/05/IFA- "
              "Form-B.pdf). Also https://x.gov/b.pdf</td>")
    after, n = linkify_urls(before)
    assert n == 2
    assert content_stream(after) == content_stream(before)
    assert strict_stream(after) == strict_stream(before)
