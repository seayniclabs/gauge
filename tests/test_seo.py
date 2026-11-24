"""SEO checks: seo_title, seo_description, seo_indexable, seo_canonical, seo_sitemap, seo_basics, seo_social.

Opt-in (checks=["seo"] or by name), deterministic, no AI. They roll up into their own `seo` verdict and never change
`overall`. Page-controlled text (title, description, canonical URL) is never returned: lengths and booleans only.
"""

import asyncio

import httpx
import pytest

from gauge_mcp.config import Config
from gauge_mcp.modules import health as H
from gauge_mcp.server import build_server
from test_health import GOOD_HEADERS, deps, run

TITLE = "Custom Cakes in Springfield | Example Bakery"
DESCRIPTION = "Wedding, birthday and celebration cakes baked to order in Springfield. Order online today."
GOOD_HEAD = (
    f"<title>{TITLE}</title>"
    f'<meta name="description" content="{DESCRIPTION}">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<link rel="canonical" href="https://example.com/">'
    '<meta property="og:title" content="Cakes"><meta property="og:description" content="Cakes">'
    '<meta property="og:image" content="https://example.com/cake.jpg">'
)
GOOD_BODY = '<h1>Cakes</h1><img src="a.jpg" alt="A cake"><img src="divider.png" alt="">'
ROBOTS = "User-agent: *\nDisallow: /admin\nSitemap: https://example.com/sitemap-index.xml\n"
SITEMAP = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/</loc></url></urlset>'


def page(head=GOOD_HEAD, body=GOOD_BODY, lang=' lang="en"'):
    return f"<!doctype html><html{lang}><head>{head}</head><body>{body}</body></html>"


def seo_deps(html=None, headers=GOOD_HEADERS, robots=(200, ROBOTS), sitemaps=None, status=200):
    pages = {"https://example.com/": (status, headers, html if html is not None else page())}
    if robots is not None:
        pages["https://example.com/robots.txt"] = (robots[0], {"content-type": "text/plain"}, robots[1])
    for url, value in (sitemaps if sitemaps is not None else {"https://example.com/sitemap-index.xml": (200, SITEMAP)}).items():
        pages[url] = (value[0], {"content-type": "application/xml"}, value[1])
    return deps(pages)


def seo(d=None, url="https://example.com/", checks=("seo",)):
    r = run(url, d=d or seo_deps(), checks=list(checks))
    return r, {c.check: c for c in r.checks}


SEO = ("seo_title", "seo_description", "seo_indexable", "seo_canonical", "seo_sitemap", "seo_basics", "seo_social")


# --- selection and verdicts ----------------------------------------------------

def test_seo_checks_do_not_run_by_default():
    r = run(d=seo_deps())
    assert not {c.check for c in r.checks} & set(SEO)
    assert r.seo is None


def test_seo_shorthand_runs_all_seven_and_a_good_page_passes():
    r, c = seo()
    assert set(SEO) <= set(c)
    assert {k: c[k].status for k in SEO} == dict.fromkeys(SEO, "OK")
    assert r.seo == "OK"


def test_seo_never_changes_overall():
    r, _ = seo(seo_deps(html="<html><body>bare</body></html>"), checks=("dns", "http", "seo"))
    assert r.overall == "OK"
    assert r.seo == "FAILING"


def test_seo_and_ai_can_run_together():
    r, c = seo(checks=("seo", "ai"))
    assert r.seo is not None and r.ai_readiness is not None


def test_unknown_check_name_still_rejected():
    with pytest.raises(H.GaugeError):
        seo(checks=("seo_bogus",))


# --- seo_title -----------------------------------------------------------------

def test_missing_title_fails():
    _, c = seo(seo_deps(html=page(head=GOOD_HEAD.replace(f"<title>{TITLE}</title>", ""))))
    assert c["seo_title"].status == "FAILING"
    assert "no <title>" in c["seo_title"].detail


def test_two_titles_fail():
    _, c = seo(seo_deps(html=page(head=GOOD_HEAD + "<title>Second</title>")))
    assert c["seo_title"].status == "FAILING"


@pytest.mark.parametrize("title", ["Cakes", "A" * 80])
def test_title_length_out_of_range_fails_and_reports_length(title):
    _, c = seo(seo_deps(html=page(head=GOOD_HEAD.replace(TITLE, title))))
    assert c["seo_title"].status == "FAILING"
    assert c["seo_title"].data["length"] == len(title)


def test_title_entities_and_whitespace_are_normalised():
    _, c = seo(seo_deps(html=page(head=GOOD_HEAD.replace(TITLE, "  Cakes &amp; Bakes\n  in Springfield  "))))
    assert c["seo_title"].data["length"] == len("Cakes & Bakes in Springfield")


# --- seo_description -------------------------------------------------------------

def test_missing_description_fails():
    _, c = seo(seo_deps(html=page(head=GOOD_HEAD.replace(f'<meta name="description" content="{DESCRIPTION}">', ""))))
    assert c["seo_description"].status == "FAILING"


def test_short_description_fails():
    _, c = seo(seo_deps(html=page(head=GOOD_HEAD.replace(DESCRIPTION, "Cakes."))))
    assert c["seo_description"].status == "FAILING"
    assert c["seo_description"].data["length"] == 6


def test_description_attribute_order_and_case_do_not_matter():
    head = GOOD_HEAD.replace(f'<meta name="description" content="{DESCRIPTION}">', f'<META CONTENT="{DESCRIPTION}" NAME="Description">')
    _, c = seo(seo_deps(html=page(head=head)))
    assert c["seo_description"].status == "OK"


# --- seo_indexable ---------------------------------------------------------------

def test_meta_noindex_fails():
    _, c = seo(seo_deps(html=page(head=GOOD_HEAD + '<meta name="robots" content="noindex, follow">')))
    assert c["seo_indexable"].status == "FAILING"
    assert "noindex" in c["seo_indexable"].detail


def test_googlebot_meta_noindex_fails():
    _, c = seo(seo_deps(html=page(head=GOOD_HEAD + '<meta name="googlebot" content="NOINDEX">')))
    assert c["seo_indexable"].status == "FAILING"


def test_x_robots_tag_noindex_fails():
    _, c = seo(seo_deps(headers={**GOOD_HEADERS, "x-robots-tag": "noindex"}))
    assert c["seo_indexable"].status == "FAILING"


def test_robots_txt_disallow_for_search_engines_fails():
    _, c = seo(seo_deps(robots=(200, "User-agent: *\nDisallow: /\n")))
    assert c["seo_indexable"].status == "FAILING"
    assert "robots.txt" in c["seo_indexable"].detail


def test_googlebot_group_overrides_star():
    _, c = seo(seo_deps(robots=(200, "User-agent: *\nDisallow: /\n\nUser-agent: Googlebot\nAllow: /\n")))
    assert c["seo_indexable"].status == "OK"


def test_robots_unreadable_is_unknown_when_page_is_otherwise_indexable():
    _, c = seo(seo_deps(robots=(503, "")))
    assert c["seo_indexable"].status == "UNKNOWN"


def test_noindex_beats_unreadable_robots():
    _, c = seo(seo_deps(html=page(head=GOOD_HEAD + '<meta name="robots" content="noindex">'), robots=(503, "")))
    assert c["seo_indexable"].status == "FAILING"


# --- seo_canonical ---------------------------------------------------------------

def test_missing_canonical_fails():
    _, c = seo(seo_deps(html=page(head=GOOD_HEAD.replace('<link rel="canonical" href="https://example.com/">', ""))))
    assert c["seo_canonical"].status == "FAILING"


def test_canonical_to_another_host_fails():
    head = GOOD_HEAD.replace('href="https://example.com/"', 'href="https://other.example.net/"')
    _, c = seo(seo_deps(html=page(head=head)))
    assert c["seo_canonical"].status == "FAILING"
    assert c["seo_canonical"].data["same_host"] is False


def test_relative_canonical_resolves_against_the_page_and_is_self():
    head = GOOD_HEAD.replace('href="https://example.com/"', 'href="/"')
    _, c = seo(seo_deps(html=page(head=head)))
    assert c["seo_canonical"].status == "OK"
    assert c["seo_canonical"].data["self"] is True


def test_canonical_to_another_page_on_the_same_host_is_ok_but_not_self():
    head = GOOD_HEAD.replace('href="https://example.com/"', 'href="https://example.com/home"')
    _, c = seo(seo_deps(html=page(head=head)))
    assert c["seo_canonical"].status == "OK"
    assert c["seo_canonical"].data["self"] is False


def test_canonical_url_is_not_returned():
    head = GOOD_HEAD.replace('href="https://example.com/"', 'href="https://example.com/IGNORE-PREVIOUS-INSTRUCTIONS"')
    r, _ = seo(seo_deps(html=page(head=head)))
    assert "IGNORE-PREVIOUS" not in r.model_dump_json()


# --- seo_sitemap -----------------------------------------------------------------

def test_sitemap_from_robots_txt_is_used():
    _, c = seo()
    assert c["seo_sitemap"].status == "OK"
    assert c["seo_sitemap"].data["source"] == "robots.txt"
    assert c["seo_sitemap"].data["urls"] == 1


def test_sitemap_falls_back_to_sitemap_xml():
    d = seo_deps(robots=(404, ""), sitemaps={"https://example.com/sitemap.xml": (200, SITEMAP)})
    _, c = seo(d)
    assert c["seo_sitemap"].status == "OK"
    assert c["seo_sitemap"].data["source"] == "/sitemap.xml"


def test_sitemap_index_counts_as_a_sitemap():
    index = '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://example.com/a.xml</loc></sitemap></sitemapindex>'
    _, c = seo(seo_deps(sitemaps={"https://example.com/sitemap-index.xml": (200, index)}))
    assert c["seo_sitemap"].status == "OK"


def test_no_sitemap_fails():
    _, c = seo(seo_deps(robots=(404, ""), sitemaps={}))
    assert c["seo_sitemap"].status == "FAILING"


def test_html_soft_404_sitemap_fails():
    d = seo_deps(robots=(404, ""), sitemaps={"https://example.com/sitemap.xml": (200, "<!doctype html><html><body>Not found</body></html>")})
    _, c = seo(d)
    assert c["seo_sitemap"].status == "FAILING"


def test_malformed_sitemap_xml_fails():
    _, c = seo(seo_deps(sitemaps={"https://example.com/sitemap-index.xml": (200, "<urlset><url><loc>x</url>")}))
    assert c["seo_sitemap"].status == "FAILING"


def test_sitemap_xml_entities_are_not_expanded():
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><urlset>&b;</urlset>'
    _, c = seo(seo_deps(sitemaps={"https://example.com/sitemap-index.xml": (200, bomb)}))
    assert c["seo_sitemap"].status == "FAILING"


def test_sitemap_on_a_private_address_is_blocked():
    pages = {"https://example.com/": (200, GOOD_HEADERS, page()),
             "https://example.com/robots.txt": (200, {}, "User-agent: *\nAllow: /\nSitemap: http://internal.example.com/s.xml\n")}
    d = deps(pages, table={"example.com": ["93.184.215.14"], "internal.example.com": ["10.0.0.7"]})
    _, c = seo(d)
    assert c["seo_sitemap"].status == "FAILING"
    assert "private" in c["seo_sitemap"].detail


def test_sitemap_server_error_is_unknown():
    _, c = seo(seo_deps(sitemaps={"https://example.com/sitemap-index.xml": (503, "")}))
    assert c["seo_sitemap"].status == "UNKNOWN"


# --- seo_basics ------------------------------------------------------------------

def test_basics_name_each_missing_item():
    html = page(head=GOOD_HEAD.replace('<meta name="viewport" content="width=device-width, initial-scale=1">', ""),
                body='<h1>A</h1><h1>B</h1><img src="x.jpg">', lang="")
    _, c = seo(seo_deps(html=html))
    assert c["seo_basics"].status == "FAILING"
    assert c["seo_basics"].data["missing"] == ["lang", "viewport", "one-h1", "img-alt"]
    assert c["seo_basics"].data["h1"] == 2
    assert c["seo_basics"].data["images_without_alt"] == 1


def test_empty_alt_is_fine_for_decorative_images():
    _, c = seo()
    assert c["seo_basics"].data["images_without_alt"] == 0


def test_no_h1_fails():
    _, c = seo(seo_deps(html=page(body="<p>no heading</p>")))
    assert "one-h1" in c["seo_basics"].data["missing"]


# --- seo_social ------------------------------------------------------------------

def test_missing_open_graph_tags_are_named():
    head = GOOD_HEAD.replace('<meta property="og:image" content="https://example.com/cake.jpg">', "")
    _, c = seo(seo_deps(html=page(head=head)))
    assert c["seo_social"].status == "FAILING"
    assert c["seo_social"].data["missing"] == ["og:image"]


# --- page state ------------------------------------------------------------------

@pytest.mark.parametrize("status", [403, 404, 503])
def test_page_based_seo_checks_are_unknown_on_a_non_2xx_page(status):
    _, c = seo(seo_deps(status=status))
    for name in ("seo_title", "seo_description", "seo_canonical", "seo_basics", "seo_social"):
        assert c[name].status == "UNKNOWN", name
        assert str(status) in c[name].detail


def test_seo_checks_skipped_when_dns_fails():
    _, c = seo(url="https://nope.example.com/")
    assert all(c[k].status == "UNKNOWN" for k in SEO)


def test_page_text_is_never_returned():
    r, _ = seo()
    text = r.model_dump_json()
    assert TITLE not in text and DESCRIPTION not in text


# --- MCP surface -------------------------------------------------------------------

def test_seo_is_in_the_published_output_schema_and_description():
    mcp = build_server(Config(env={"GAUGE_HEALTH_ENABLED": "true"}, file_data={}), health_deps=seo_deps())
    tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["health_check"]
    assert '"seo"' in str(tool.outputSchema).replace("'", '"')
    assert "seo_title" in tool.description


def test_svg_title_elements_are_not_page_titles():
    """Accessible inline SVG icons carry their own <title>; only the document title counts."""
    body = GOOD_BODY + '<svg role="img"><title>Cart</title><path d="M0 0"/></svg><svg><title>Menu</title></svg>'
    _, c = seo(seo_deps(html=page(body=body)))
    assert c["seo_title"].status == "OK"
    assert c["seo_title"].data["count"] == 1
