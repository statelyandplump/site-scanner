#!/usr/bin/env python3
"""
Positive controls for site_scan's checks.

A real crawl only proves the checks that happened to fire. Everything else is
untested, and a check that never fires is indistinguishable from a check that
cannot fire. These feed known-bad HTML through the same parser and finding
builder the scanner uses and assert each issue is raised.

    python test_checks.py
"""

import hashlib
import json
import math
import os
import sys

import site_scan as S


class Opts(object):
    external = True
    image_kb = 200
    dupe_ratio = 0.90
    workers = 4
    timeout = 10


FAILS = []


def check(name, condition, detail=""):
    if condition:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILS.append(name)


# ---------------------------------------------------------------- parser

PAGE = """
<!doctype html><html lang="en-US"><head>
<title>Anxiety Therapy in Denver</title>
<meta name="description" content="Short one.">
<link rel="canonical" href="https://ex.com/anxiety/">
</head><body>
<nav><a href="/">Home</a><img src="/logo.png"></nav>
<h1>Anxiety Therapy</h1>
<h1>Second H1</h1>
<p>We help adults manage anxiety with evidence based care in Denver Colorado.</p>
<img src="/team.jpg">
<img src="/hero.jpg" alt="Our office">
<img src="/spacer.gif" alt="">
<img src="/meaningful.jpg" alt="">
<a href="/contact/">Contact</a>
<a href="https://external.example/thing">Outbound</a>
<a href="#skip">Anchor</a>
<a href="mailto:a@b.com">Mail</a>
<script>var x = "not page text";</script>
<footer><a href="/privacy/">Privacy</a></footer>
</body></html>
"""


def test_parser():
    print("\nparser")
    p = S.parse_page(PAGE)
    check("title", p.title == "Anxiety Therapy in Denver", repr(p.title))
    check("meta description", p.meta_desc == "Short one.", repr(p.meta_desc))
    check("canonical", p.canonical == "https://ex.com/anxiety/", repr(p.canonical))
    check("lang", p.lang == "en-US", repr(p.lang))
    check("both H1s captured", len(p.h1s) == 2, p.h1s)

    by_src = {i["src"]: i for i in p.images}
    check("logo in nav still collected", "/logo.png" in by_src)
    check("missing alt flagged", by_src["/team.jpg"]["alt_missing"] is True)
    check("present alt not flagged", by_src["/hero.jpg"]["alt_missing"] is False)
    check("empty alt flagged as empty", by_src["/meaningful.jpg"]["alt_empty"] is True)
    check("spacer.gif read as decorative", by_src["/spacer.gif"]["decorative"] is True)

    hrefs = [l["href"] for l in p.links]
    check("real links kept", "/contact/" in hrefs and "/privacy/" in hrefs)
    check("script text excluded", "not page text" not in p.text, p.text[:80])
    check("nav text excluded from content", "Home" not in p.text, p.text[:80])
    check("footer text excluded from content", "Privacy" not in p.text, p.text[:80])
    check("body copy kept", "evidence based care" in p.text, p.text[:80])


def test_url_normalising():
    print("\nurl handling")
    base = "https://ex.com/a/b/"
    check("relative resolved",
          S.norm_url("../c/", base) == "https://ex.com/a/c/")
    check("fragment dropped",
          S.norm_url("/x/#frag", base) == "https://ex.com/x/")
    check("mailto rejected", S.norm_url("mailto:a@b.com", base) is None)
    check("bare anchor rejected", S.norm_url("#top", base) is None)
    check("javascript rejected", S.norm_url("javascript:void(0)", base) is None)
    # The bug the yoast run surfaced: an en-dash in a filename must survive.
    endash = S.safe_url("https://ex.com/up/Keyword–Planner.png")
    check("en-dash percent-encoded", "%E2%80%93" in endash, endash)
    check("already-encoded not double-encoded",
          S.safe_url("https://ex.com/a%20b.png") == "https://ex.com/a%20b.png")
    check("plain url untouched",
          S.safe_url("https://ex.com/a/b?c=1") == "https://ex.com/a/b?c=1")


# ---------------------------------------------------------------- duplicates

BOILER = ("Call us today to book a free consultation with our team of "
          "licensed clinicians serving the whole metro area. ")
BODY_A = ("Cognitive behavioural therapy helps you notice the thoughts that keep "
          "anxiety running and gives you tools to interrupt them in daily life. ") * 3
BODY_B = ("Couples counselling gives partners a structured place to slow down "
          "arguments and rebuild the trust that distance wears away over years. ") * 3


def page(url, text):
    return {"url": url, "shingles": S.shingles(text), "words": len(text.split())}


def flatten(groups):
    found = set()
    for g in groups:
        found.add(g["primary"]["url"])
        found.update(b["url"] for b, _ in g["matches"])
    return found


# Page bodies have to be realistic *length*, not just realistic prose. Shingle
# overlap is length-sensitive: swapping one word kills the 6 windows spanning it,
# so on a 35-word stub two near-identical pages score ~0.5, while on a 350-word
# page they score ~0.94. An unrealistically short fixture fails a working check.
# Deliberately larger than TOPIC_MIN_TERMS by a wide margin. An earlier version
# had ~40 words, so every generated page landed right at the distinct-term floor
# and got filtered — which made three separate checks look broken when the
# fixtures were the problem.
LEXICON = ("therapy support anxiety depression trauma couples grief parenting "
           "sessions clinician practice referral intake insurance evening weekend "
           "telehealth office appointment consultation approach evidence outcomes "
           "adolescents adults veterans clinicians training supervision community "
           "recovery burnout boundaries attachment nervous system regulation "
           "assessment diagnosis medication psychiatry mindfulness somatic "
           "exposure rumination avoidance compassion resilience caregiver "
           "postpartum bereavement identity transition workplace conflict "
           "communication trust intimacy separation reconciliation coaching "
           "adhd autism sensory processing sleep nutrition movement journaling "
           "worksheet homework relapse prevention coping distress tolerance "
           "validation attunement rupture repair alliance outcome measure "
           "screening waitlist scheduling reminder cancellation policy fee "
           "sliding scale superbill reimbursement deductible copay network").split()


def prose(seed, words=340):
    """Deterministic pseudo-prose, unique per seed, long enough to be realistic."""
    out, n = [], seed * 7919 + 13
    for i in range(words):
        n = (n * 1103515245 + 12345) % 2147483648
        out.append(LEXICON[(n + i * seed) % len(LEXICON)])
    return " ".join(out)


# A realistic content page: a distinct body of its own on top of shared chrome.
def service_page(n):
    return BOILER + prose(n + 101)


def test_duplicates():
    print("\nduplicate content")

    # Small corpus: two identical bodies plus shared chrome -> duplicate.
    dupes = [page("https://ex.com/a/", BOILER + BODY_A),
             page("https://ex.com/b/", BOILER + BODY_A),
             page("https://ex.com/c/", BOILER + BODY_B)]
    check("identical bodies grouped (small site)",
          flatten(S.find_duplicates(dupes, 0.90)[0])
          == {"https://ex.com/a/", "https://ex.com/b/"},
          flatten(S.find_duplicates(dupes, 0.90)[0]))

    # Distinct bodies sharing only chrome must NOT group, at either corpus size.
    for size, label in ((5, "small site"), (20, "large site")):
        distinct = [page("https://ex.com/s%d/" % i, service_page(i)) for i in range(size)]
        groups = S.find_duplicates(distinct, 0.90)[0]
        check("distinct pages not grouped (%s)" % label, groups == [], flatten(groups))

    # The real case: templated city pages where only the town name changes,
    # sitting inside a corpus of genuinely distinct pages. The cluster is large
    # enough to look like boilerplate to a naive threshold.
    cities = ["Denver", "Aurora", "Lakewood", "Boulder", "Littleton",
              "Arvada", "Westminster", "Thornton", "Centennial", "Golden",
              "Parker", "Brighton"]
    shared_body = prose(999)        # the identical copy every city page reuses
    geo_tpl = (BOILER + "Our %s office offers in person and online therapy for "
               "adults across the metro area. " + shared_body +
               " Book a free consultation with a licensed clinician near %s today.")
    corpus = [page("https://ex.com/therapy-%s/" % c.lower(), geo_tpl % (c, c))
              for c in cities]
    corpus += [page("https://ex.com/s%d/" % i, service_page(i)) for i in range(8)]

    geo_found = flatten(S.find_duplicates(corpus, 0.90)[0])
    geo_urls = {"https://ex.com/therapy-%s/" % c.lower() for c in cities}
    check("templated city pages caught", geo_found >= geo_urls,
          "missed: %s" % (geo_urls - geo_found))
    check("distinct pages not swept in", not (geo_found - geo_urls),
          "false positives: %s" % (geo_found - geo_urls))


# ---------------------------------------------------------------- findings

def synthetic_pages():
    """Two pages sharing a title, description, and a missing-alt image."""
    common = {
        "status": 200, "error": None, "redirected": False, "meta_robots": "",
        "canonical": "", "lang": "en", "links": [], "noindex": False,
    }
    img_bad = {"src": "/team.jpg", "target": "https://ex.com/team.jpg",
               "alt": "", "alt_missing": True, "alt_empty": False,
               "decorative": False, "width": "", "height": "", "lazy": False}
    p1 = dict(common, url="https://ex.com/a/", final_url="https://ex.com/a/",
              title="Same Title Everywhere", meta_desc="Same description everywhere.",
              h1s=[], words=40, text_hash="x", shingles=set(), images=[img_bad])
    p2 = dict(common, url="https://ex.com/b/", final_url="https://ex.com/b/",
              title="Same Title Everywhere", meta_desc="Same description everywhere.",
              h1s=["One", "Two"], words=900, text_hash="y", shingles=set(),
              images=[img_bad])
    p3 = dict(common, url="https://ex.com/dead/", final_url="https://ex.com/dead/",
              status=404, skipped=True)
    p4 = dict(common, url="https://ex.com/timeout/", final_url="https://ex.com/timeout/",
              status=None, error="timed out", skipped=True)
    return [p1, p2, p3, p4]


def test_findings():
    print("\nfindings")
    pages = synthetic_pages()
    assets = {"https://ex.com/team.jpg": S.Resp(
        "https://ex.com/team.jpg", status=200, length=1_500_000, ctype="image/jpeg")}
    f, live, _ = S.build_findings(pages, assets, Opts())

    def n(key):
        return len(f.get(key) or [])

    check("404 page reported", n("page_error") == 1, f.get("page_error"))
    check("unreachable page reported", n("unreachable") == 1, f.get("unreachable"))
    check("duplicate title reported", n("title_duplicate") == 1)
    check("duplicate description reported", n("desc_duplicate") == 1)
    check("missing H1 reported", n("h1_missing") == 1)
    check("multiple H1s reported", n("h1_multiple") == 1)
    check("thin page reported", n("thin") == 1)
    check("missing alt reported once, not per page", n("alt_missing") == 1,
          f.get("alt_missing"))
    check("missing alt names both pages",
          len((f["alt_missing"][0]).get("group", [])) == 2, f.get("alt_missing"))
    check("1.5 MB image reported as huge", n("image_huge") == 1, f.get("image_huge"))
    check("huge image not double-counted as heavy", n("image_heavy") == 0)
    check("clean pages produce no title_missing", n("title_missing") == 0)

    # A clean page set must produce nothing. A checker that always fires is noise.
    clean = [dict(synthetic_pages()[0], url="https://ex.com/only/",
                  title="A Perfectly Reasonable Page Title Here",
                  meta_desc="A meta description of an entirely respectable length "
                            "that says what the page is about.",
                  h1s=["One heading"], words=800, images=[])]
    f2, _, _ = S.build_findings(clean, {}, Opts())
    noisy = [k for k, _, _, _ in S.ISSUES if f2.get(k)]
    check("clean page raises nothing", not noisy, noisy)


def test_csv_columns():
    """Every finding must land in typed columns, not a prose blob."""
    print("\ncsv columns")
    import csv as _csv
    import io
    import tempfile

    pages = synthetic_pages()
    pages[0]["title"] = "A" * 90                 # long title
    pages[1]["title"] = "A" * 90
    pages[0]["meta_desc"] = "d" * 200            # long description
    assets = {"https://ex.com/team.jpg": S.Resp(
        "https://ex.com/team.jpg", status=200, length=1_500_000, ctype="image/jpeg")}
    f, _, _ = S.build_findings(pages, assets, Opts())

    path = os.path.join(tempfile.mkdtemp(), "out.csv")
    S.write_csv(path, f)
    with open(path, encoding="utf-8-sig") as fh:
        rows = list(_csv.DictReader(fh))

    check("header matches CSV_COLUMNS",
          list(rows[0].keys()) == S.CSV_COLUMNS, list(rows[0].keys()))
    check("opens as utf-8-sig so Excel reads accents",
          open(path, "rb").read(3) == b"\xef\xbb\xbf")

    by_issue = {}
    for r in rows:
        by_issue.setdefault(r["Issue"], []).append(r)

    title_rows = by_issue.get("Titles over %d characters" % S.TITLE_MAX, [])
    check("long title has a numeric measurement",
          title_rows and title_rows[0]["Measurement"].isdigit(), title_rows[:1])
    check("long title names its unit",
          title_rows and title_rows[0]["Unit"] == "characters", title_rows[:1])
    check("long title carries the actual title text",
          title_rows and title_rows[0]["Content"].startswith("A"), title_rows[:1])

    img = by_issue.get("Images over 1 MB", [])
    check("oversized image measured in KB",
          img and img[0]["Unit"] == "KB" and img[0]["Measurement"].isdigit(), img[:1])
    check("image row names a page it appears on",
          img and img[0]["Example page"].startswith("http"), img[:1])

    alt = by_issue.get("Images with no alt attribute", [])
    check("alt row counts affected pages",
          alt and alt[0]["Pages affected"] == "2", alt[:1])

    err = by_issue.get("Pages returning an error", [])
    check("error page carries its status code",
          err and err[0]["Status"] == "404", err[:1])

    # Nothing may smuggle several values into one cell.
    for r in rows:
        if r["Measurement"] and not str(r["Measurement"]).replace(".", "").isdigit():
            check("measurement column is numeric only", False, r)
            break
    else:
        check("measurement column is numeric only", True)


def test_import_surface():
    """The scan must be usable by an importer, not only by the CLI.

    A reporting pipeline may always include this scan, but the scan also runs
    on its own. Both callers share one implementation and neither goes through a
    subprocess. If this section fails, the two have drifted.
    """
    print("\nimport surface")
    check("scan() is importable", callable(getattr(S, "scan", None)))
    check("NoSitemap is an exception, not an exit code",
          isinstance(getattr(S, "NoSitemap", None), type)
          and issubclass(S.NoSitemap, Exception))

    # Defaults are part of the contract: a consumer thresholds against them.
    o = S.Options()
    check("default image budget is 100 KB", o.image_kb == 100, o.image_kb)
    check("default banner budget is 200 KB", o.banner_kb == 200, o.banner_kb)
    check("Options accepts kwargs", S.Options(limit=5, external=False).limit == 5)

    # evidence() built from a known ScanResult, no network involved.
    pages = synthetic_pages()
    assets = {"https://ex.com/team.jpg": S.Resp(
        "https://ex.com/team.jpg", status=200, length=1_500_000, ctype="image/jpeg")}
    f, live, _ = S.build_findings(pages, assets, Opts())
    result = S.ScanResult("ex.com", "https://ex.com/sitemap.xml", 900, pages, live,
                          f, None, 0, 12.5, 42, S.Options(limit=4))
    ev = result.evidence()

    check("evidence names its source", ev["source"] == "site-scanner", ev.get("source"))
    check("evidence declares no JS rendering", ev["javascript_rendered"] is False)
    check("evidence reports the full sitemap size, not the scanned subset",
          ev["crawl"]["urls_in_sitemap"] == 900, ev["crawl"])
    check("evidence flags that the crawl was limited",
          ev["crawl"]["limited"] is True, ev["crawl"]["limited"])
    check("evidence carries the image budgets used",
          ev["images"]["budget_kb"] == 100 and ev["images"]["banner_budget_kb"] == 200)
    check("evidence separates missing alt from empty alt",
          ev["images"]["missing_alt_count"] == 1
          and "empty_alt_count" in ev["images"], ev["images"])
    check("evidence lists which pages each alt-less image is on",
          ev["images"]["missing_alt"][0]["pages"] == ["https://ex.com/a/",
                                                      "https://ex.com/b/"],
          ev["images"]["missing_alt"])
    check("evidence counts unreachable pages", ev["pages_unreachable"] == 2,
          ev["pages_unreachable"])
    check("evidence keeps unverified links apart from broken ones",
          "unverified_count" in ev["links"] and "unverified" in ev["links"],
          list(ev["links"]))

    # The COLLECT contract: facts only. Scoring lives in frameworks/*.md.
    blob = json.dumps(ev)
    leaked = [w for w in ("severity", "High", "Medium", "recommend", "should be",
                          "PASS", "FAIL", "verdict") if w in blob]
    check("evidence carries no severity or recommendation prose", not leaked, leaked)
    check("evidence is JSON-serialisable", isinstance(blob, str) and len(blob) > 100)


def test_blog_segmentation():
    """Blog posts must be labelled, and the things that matter must NOT be."""
    print("\nblog segmentation")
    for u in ("https://ex.com/blog/anxiety-tips", "https://ex.com/blog",
              "https://ex.com/news/update/", "https://ex.com/2024/06/a-post",
              "https://ex.com/articles/thing"):
        check("blog: %s" % u.replace("https://ex.com", ""), S.is_blog(u), u)
    for u in ("https://ex.com/anxiety-therapy", "https://ex.com/about",
              "https://ex.com/therapy-denver/", "https://ex.com/"):
        check("not blog: %s" % u.replace("https://ex.com", ""), not S.is_blog(u), u)
    # "/blogger-outreach" must not read as a blog post.
    check("not fooled by a substring", not S.is_blog("https://ex.com/blogger-outreach"))

    pages = synthetic_pages()
    pages[0]["url"] = "https://ex.com/blog/a-post"
    f, _, _ = S.build_findings(pages, {}, Opts())
    h1 = f.get("h1_missing") or []
    check("meta findings carry a blog flag", all("blog" in r for r in h1), h1)

    # The load-bearing one: a broken link found on a blog page is still a broken
    # link. On a real blog-heavy site, segmenting these would have buried eight
    # 404ing category links that were the best finding on the site.
    for key in ("link_broken", "alt_missing", "image_heavy", "image_huge"):
        check("%s is not blog-segmented" % key, key not in S.BLOG_SEGMENTED)


def test_topic_overlap():
    """Same topic in different words — the case shingles cannot see."""
    print("\ntopic overlap")

    def pg(url, text):
        return {"url": url, "final_url": url, "canonical": "", "noindex": False,
                "words": len(text.split()), "terms": S.terms_of(text),
                "shingles": S.shingles(text),
                "text_hash": hashlib.sha1(text.encode()).hexdigest()}

    # Same subject, deliberately almost no shared phrasing.
    a = pg("https://ex.com/anxiety-therapy/", """
        Anxiety therapy helps adults who feel constant worry, racing thoughts and
        physical tension. Our clinicians treat panic attacks, social anxiety and
        generalised anxiety using cognitive behavioural techniques and exposure
        work. Sessions focus on identifying anxious thoughts, testing them, and
        building tolerance for the physical sensations of panic. Many clients
        notice their worry becoming less constant within a few months of weekly
        anxiety treatment with a licensed clinician.""")
    b = pg("https://ex.com/help-with-worry/", """
        Struggling with worry? Treatment for anxiety gives adults practical tools
        for racing thoughts, panic and the physical tension anxiety produces. Our
        licensed clinicians use cognitive behavioural techniques and graded
        exposure to treat panic attacks, social anxiety and generalised worry.
        Weekly sessions build tolerance for anxious physical sensations and test
        anxious thoughts directly, and most clients find constant worry eases
        within months of starting anxiety treatment.""")
    # A genuinely different subject, same page shape and length.
    c = pg("https://ex.com/couples-counselling/", """
        Couples counselling gives partners a structured place to slow down
        arguments and rebuild trust that distance wears away. Sessions focus on
        communication patterns, repair after conflict, and the resentment that
        accumulates when partners stop turning toward each other. Our clinicians
        work with married and unmarried couples, including partners considering
        separation, and help each person hear what the other is actually asking
        for during a disagreement.""")

    check("shingles miss the reworded pair (this is why TF-IDF was added)",
          S.jaccard(a["shingles"], b["shingles"]) < 0.2,
          round(S.jaccard(a["shingles"], b["shingles"]), 3))

    pairs = S.find_topic_overlap([a, b, c], threshold=0.45)
    found = {frozenset((x["url"], y["url"])) for x, y, _ in pairs}
    check("reworded same-topic pair IS caught",
          frozenset((a["url"], b["url"])) in found,
          [(x["url"], y["url"], round(s, 2)) for x, y, s in pairs])
    check("different topic is NOT caught",
          not any(c["url"] in p for p in found),
          [(x["url"], y["url"], round(s, 2)) for x, y, s in pairs])

    # Blog-vs-blog is excluded by default; several posts on one theme is normal.
    # Must clear TOPIC_MIN_TERMS distinct terms or they are skipped, not scored.
    # An earlier version of this fixture had 38 and scored 0.61 — correctly
    # filtered, which made the check look broken when it was the fixture.
    b1 = pg("https://ex.com/blog/anxiety-tips/", """
        Anxiety therapy helps adults who feel constant worry, racing thoughts and
        physical tension. Clinicians treat panic attacks, social anxiety and
        generalised anxiety using cognitive behavioural techniques and exposure
        work. Sessions identify anxious thoughts, test them, and build tolerance
        for the physical sensations of panic. Clients notice worry becoming less
        constant within months of weekly anxiety treatment. Appointments run
        evenings and weekends, insurance is accepted, and telehealth appointments
        remain available across the region for anyone unable to travel.""")
    b2 = pg("https://ex.com/blog/managing-worry/", """
        Struggling with worry? Treatment for anxiety gives adults practical tools
        for racing thoughts, panic and physical tension. Licensed clinicians use
        cognitive behavioural techniques and graded exposure for panic attacks,
        social anxiety and generalised worry. Weekly sessions build tolerance for
        anxious physical sensations and test anxious thoughts, and constant worry
        eases within months of starting anxiety treatment. Evening and weekend
        appointments are available, insurance accepted, with telehealth across
        the region for anyone who cannot travel to the office.""")
    blog_only = S.find_topic_overlap([b1, b2], threshold=0.3)
    check("blog-vs-blog excluded by default", blog_only == [], blog_only)
    check("blog-vs-blog included on request",
          len(S.find_topic_overlap([b1, b2], threshold=0.3, include_blog=True)) == 1)

    # Clusters, not pairs: N templated pages are one finding, not N-choose-2.
    towns = ["denver", "aurora", "lakewood", "boulder", "littleton"]
    tpl = ("Our %s office offers therapy for adults across the metro area. "
           "Clinicians in %s treat anxiety, depression, trauma and relationship "
           "difficulties, with evening and weekend appointments, insurance "
           "accepted, and telehealth for anyone unable to travel. Book a free "
           "consultation with a licensed clinician near %s today and begin "
           "feeling steadier within weeks of starting regular sessions. Parking "
           "is available onsite and the building has step-free access "
           "throughout, including accessible bathrooms near reception.")
    geo = [pg("https://ex.com/therapy-%s/" % t, tpl % (t, t, t)) for t in towns]
    clusters = S.topic_clusters(geo + [c], threshold=0.5)
    check("templated pages collapse to ONE cluster, not 10 pairs",
          len(clusters) == 1, [(len(m), round(h, 2)) for m, l, h in clusters])
    check("the cluster names every member",
          clusters and len(clusters[0][0]) == 5, clusters and clusters[0][0])
    check("the unrelated page stays out of the cluster",
          clusters and c["url"] not in clusters[0][0], clusters and clusters[0][0])
    check("cluster reports a score range",
          clusters and 0 < clusters[0][1] <= clusters[0][2] <= 1.0,
          clusters and clusters[0][1:])

    # Regression guard for the self-erasure bug: a corpus that is ENTIRELY one
    # template must still score high. Dropping terms present on every page made
    # this exactly 0.0, and only on the small sites the check matters most for.
    only_geo = S.topic_clusters(geo, threshold=0.5)
    check("an all-template corpus still scores (no self-erasure)",
          len(only_geo) == 1 and len(only_geo[0][0]) == 5,
          [(len(m), round(h, 2)) for m, l, h in only_geo])
    vecs = S.tfidf_vectors([{"terms": p["terms"]} for p in geo])
    score = S.cosine(vecs[0], vecs[1])
    # Not near 1.0, and correctly so: with a tiny corpus the one differing term
    # (the town) has df=1 and carries far more IDF weight than the shared
    # vocabulary at df=n. What matters is that it clears the default threshold.
    check("terms on every page keep a usable weight",
          score > S.TOPIC_RATIO, round(score, 3))

    check("too-short pages are skipped, not scored",
          S.find_topic_overlap([pg("https://ex.com/x/", "short page"),
                                pg("https://ex.com/y/", "short page")]) == [])

    # Wording must matter, not just length: identical vocabulary, opposite order
    # should still score high (bag of words), which is the documented tradeoff.
    check("stopwords excluded from terms", "the" not in S.terms_of("the cat sat"))
    check("short tokens excluded", "at" not in S.terms_of("sat at mat"))
    check("real terms kept", "anxiety" in S.terms_of("anxiety therapy helps"))


def test_similarity_map():
    """The map has to be reproducible, self-contained, and geometrically honest."""
    print("\nsimilarity map")
    pages = [{"url": "https://ex.com/p%d/" % i, "words": 340,
              "terms": S.terms_of(service_page(i))} for i in range(24)]
    check("fixtures clear the distinct-term floor",
          min(len(p["terms"]) for p in pages) >= S.TOPIC_MIN_TERMS,
          min(len(p["terms"]) for p in pages))

    nodes, edges, neighbours = S.similarity_graph(pages)
    check("graph has nodes", len(nodes) == 24, len(nodes))
    check("neighbours are capped per page",
          all(len(v) <= S.NEIGHBOURS_PER_PAGE for v in neighbours.values()))
    check("neighbour indices are valid against the returned node list",
          all(0 <= j < len(nodes) for v in neighbours.values() for _, j in v))
    check("neighbours are sorted strongest first",
          all(list(v) == sorted(v, reverse=True) for v in neighbours.values()))
    check("no page is its own neighbour",
          all(i != j for i, v in neighbours.items() for _, j in v))
    check("edges are capped per node, not every pair",
          len(edges) <= 24 * S.MAP_EDGES_PER_NODE, len(edges))
    check("every edge carries a weight above the floor",
          all(w >= S.MAP_EDGE_FLOOR for _, _, w in edges))
    check("no self-edges", all(a != b for a, b, _ in edges))

    # Determinism is the whole reason the layout seeds on a spiral, not an RNG:
    # the same site scanned twice must draw the same picture.
    first = S.layout_force(len(nodes), edges)
    second = S.layout_force(len(nodes), edges)
    check("layout is deterministic across runs", first == second)
    check("layout produces finite coordinates",
          all(math.isfinite(x) and math.isfinite(y) for x, y in first))
    check("nodes do not all collapse to one point",
          len({(round(x, 3), round(y, 3)) for x, y in first}) > len(first) // 2,
          len({(round(x, 3), round(y, 3)) for x, y in first}))

    flagged = [pages[0]["url"], pages[1]["url"]]
    svg, near_rows = S.render_map_svg(pages, [flagged])

    # The map must be usable, not just pretty: every mark opens its page, and
    # the rows below say what to compare with what.
    check("every mark is a link", svg.count("<a href=") == len(nodes),
          (svg.count("<a href="), len(nodes)))
    check("links open in a new tab safely", 'rel="noopener"' in svg)
    check("tooltips name the closest matches", "Closest:" in svg)
    check("tooltips say the mark is clickable", "Click to open" in svg)
    check("a neighbour table comes back with the svg", len(near_rows) > 0)
    check("table rows are sorted strongest match first",
          [r["top"] for r in near_rows] == sorted(
              (r["top"] for r in near_rows), reverse=True))
    check("each row names its matches",
          all(r["matches"] and all(isinstance(p, str) for p, _ in r["matches"])
              for r in near_rows))
    check("flagged pages are marked in the table",
          any(r["flagged"] for r in near_rows))
    check("svg renders", svg.startswith("<svg") and svg.endswith("</svg>"))
    check("svg is self-contained — no script", "<script" not in svg)
    check("svg has no external references",
          "http://" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', ""))
    check("svg carries an aria-label for screen readers", 'aria-label=' in svg)
    check("colours come from CSS vars, not baked hex", "#" not in svg, svg[:200])
    check("every mark has a hover title", svg.count("<title>") == len(nodes),
          (svg.count("<title>"), len(nodes)))
    check("hover targets are larger than the marks", 'class="hit"' in svg)
    # ONE label per cluster, not one per page — members overlap by construction.
    check("clusters get one label each, not one per page",
          svg.count("<text") == 1, svg.count("<text"))
    check("the label states how many pages", "2 pages, same topic" in svg)
    check("two clusters get two labels",
          S.render_map_svg(pages, [[pages[0]["url"], pages[1]["url"]],
                                   [pages[2]["url"], pages[3]["url"]]]
                           )[0].count("<text") == 2)
    check("blog pages are drawn as squares, not colour-only",
          "<rect" in S.render_map_svg(
              pages + [{"url": "https://ex.com/blog/x/", "words": 340,
                        "terms": S.terms_of(service_page(99))}], [flagged])[0])

    # Coordinates must stay inside the viewBox or marks clip at the edge.
    import re as _re
    coords = [float(v) for v in _re.findall(r'c[xy]="([-\d.]+)"', svg)]
    check("all marks sit inside the viewBox",
          all(-2 <= c <= max(S.MAP_W, S.MAP_H) + 2 for c in coords),
          (min(coords), max(coords)))

    check("too few pages renders nothing rather than a broken chart",
          S.render_map_svg(pages[:2], []) == ("", []))

    # Dark mode is not an afterthought: any hex outside a token block is a
    # colour that cannot swap, and the report is read in both modes. A stray
    # `details[open]{background:#fcfcfd}` shipped a white panel with white text.
    css_lines = [l for l in S.HTML_CSS.splitlines()
                 if "#" in l and not l.strip().startswith("--")]
    check("no hardcoded colours outside the theme tokens", not css_lines, css_lines)
    for token in ("--bg", "--fg", "--card", "--line", "--s1", "--s2", "--s3"):
        light = S.HTML_CSS.count(token + ":")
        check("%s is defined for light and both dark scopes" % token,
              light >= 3, light)

    # Gravity check: pages with NO edges must stay in frame, not ring the edge.
    # Without a centring force they feel only repulsion and escape to the border,
    # crushing everything that matters into the middle.
    lonely = S.layout_force(30, [])
    mid = S.LAYOUT_SPAN / 2
    spread = max(math.hypot(x - mid, y - mid) for x, y in lonely)
    check("edgeless nodes stay near the centre, not flung to a ring",
          spread < S.LAYOUT_SPAN * 0.75, round(spread, 1))


def test_indexability_and_exact():
    """Match Screaming Frog: exact dupes separately, indexable pages only."""
    print("\nexact duplicates and indexability")

    def pg(url, text, **kw):
        d = {"url": url, "final_url": url, "canonical": "", "noindex": False,
             "words": len(text.split()),
             "text_hash": hashlib.sha1(text.encode()).hexdigest(),
             "shingles": S.shingles(text)}
        d.update(kw)
        return d

    body = prose(7)
    a, b = pg("https://ex.com/a/", body), pg("https://ex.com/b/", body)
    c = pg("https://ex.com/c/", prose(8))
    groups = S.find_exact_duplicates([a, b, c])
    check("identical body text grouped", len(groups) == 1 and len(groups[0]) == 2,
          groups)
    check("different text not grouped",
          all("https://ex.com/c/" not in [p["url"] for p in g] for g in groups))

    check("plain page is indexable", S.is_indexable(a))
    check("noindex page is not", not S.is_indexable(pg("https://ex.com/n/", body,
                                                       noindex=True)))
    check("self-canonical is still indexable",
          S.is_indexable(pg("https://ex.com/s/", body,
                            canonical="https://ex.com/s/")))
    check("trailing slash does not break self-canonical",
          S.is_indexable(pg("https://ex.com/s/", body,
                            canonical="https://ex.com/s")))
    check("canonicalised elsewhere is not indexable",
          not S.is_indexable(pg("https://ex.com/dup/", body,
                                canonical="https://ex.com/original/")))

    # End to end: a canonicalised twin must not be reported as a duplicate.
    twin = pg("https://ex.com/twin/", body, canonical="https://ex.com/a/")
    f, _, _ = S.build_findings([dict(a, status=200, title="A", meta_desc="d",
                                     h1s=["A"], links=[], images=[], error=None,
                                     redirected=False, meta_robots=""),
                                dict(twin, status=200, title="B", meta_desc="e",
                                     h1s=["B"], links=[], images=[], error=None,
                                     redirected=False, meta_robots="")],
                               {}, Opts())
    check("canonicalised twin is not flagged duplicate",
          not (f.get("content_exact") or f.get("content_duplicate")),
          {k: f.get(k) for k in ("content_exact", "content_duplicate")})

    # ...but two indexable identical pages are, as exact and not also as near.
    f2, _, _ = S.build_findings([dict(a, status=200, title="A", meta_desc="d",
                                      h1s=["A"], links=[], images=[], error=None,
                                      redirected=False, meta_robots=""),
                                 dict(b, status=200, title="B", meta_desc="e",
                                      h1s=["B"], links=[], images=[], error=None,
                                      redirected=False, meta_robots="")],
                                {}, Opts())
    check("indexable identical pages flagged as exact",
          len(f2.get("content_exact") or []) == 1, f2.get("content_exact"))
    check("exact dupes not double-reported as near-duplicates",
          not f2.get("content_duplicate"), f2.get("content_duplicate"))


def test_throttle_retry():
    """A throttled response must be retried, not reported as a broken link."""
    print("\nthrottle handling")
    check("503 is retryable", 503 in S.RETRY_STATUS)
    check("429 is retryable", 429 in S.RETRY_STATUS)
    check("504 is retryable", 504 in S.RETRY_STATUS)
    check("404 is NOT retryable — it is the finding", 404 not in S.RETRY_STATUS)
    check("410 is NOT retryable", 410 not in S.RETRY_STATUS)
    check("403 is NOT retryable", 403 not in S.RETRY_STATUS)

    # Drive probe() against a stubbed opener: 503 first, 200 second.
    calls = []
    real_request, real_sleep = S._request, S.time.sleep

    class FakeResp(object):
        headers = {"Content-Length": "1234", "Content-Type": "image/jpeg"}
        status = 200

        def read(self):
            return b"x" * 1234

        def geturl(self):
            return "https://ex.com/img.jpg"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_request(url, method="GET", timeout=None, extra_headers=None):
        calls.append(method)
        if len(calls) == 1:
            raise S.urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        return FakeResp()

    S._request = fake_request
    S.time.sleep = lambda s: None          # no real wait in a test
    try:
        res = S.probe("https://ex.com/img.jpg")
    finally:
        S._request, S.time.sleep = real_request, real_sleep

    check("probe retried after the 503", len(calls) == 2, calls)
    check("retry result is the good one", res.status == 200, res.status)
    check("retry keeps the size", res.length == 1234, res.length)

    # And it must give up rather than loop.
    calls2 = []

    def always_503(url, method="GET", timeout=None, extra_headers=None):
        calls2.append(method)
        raise S.urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)

    S._request = always_503
    S.time.sleep = lambda s: None
    try:
        res2 = S.probe("https://ex.com/gone")
    finally:
        S._request, S.time.sleep = real_request, real_sleep
    check("retries up to MAX_RETRIES then stops",
          len(calls2) == S.MAX_RETRIES + 1, calls2)
    check("persistent 503 is reported", res2.status == 503, res2.status)

    # The one that matters: throttled is NOT broken.
    check("429 marks a response throttled",
          S.Resp("u", status=429).throttled is True)
    check("503 marks a response throttled",
          S.Resp("u", status=503).throttled is True)
    check("404 is not throttled", S.Resp("u", status=404).throttled is False)

    page = {"url": "https://ex.com/a/", "final_url": "https://ex.com/a/",
            "status": 200, "error": None, "redirected": False, "title": "T",
            "meta_desc": "d", "meta_robots": "", "canonical": "", "h1s": ["H"],
            "words": 900, "text_hash": "h", "shingles": set(), "images": [],
            "noindex": False,
            "links": [{"href": "/live", "anchor": "Live", "rel": "",
                       "nofollow": False, "target": "https://ex.com/live",
                       "internal": True},
                      {"href": "/dead", "anchor": "Dead", "rel": "",
                       "nofollow": False, "target": "https://ex.com/dead",
                       "internal": True}]}
    assets = {"https://ex.com/live": S.Resp("https://ex.com/live", status=429),
              "https://ex.com/dead": S.Resp("https://ex.com/dead", status=404)}
    f, _, _ = S.build_findings([page], assets, Opts())
    broken = [r["url"] for r in f.get("link_broken") or []]
    unver = [r["url"] for r in f.get("link_unverified") or []]
    check("a throttled link is NOT reported broken",
          "https://ex.com/live" not in broken, broken)
    check("a throttled link is reported as unverified",
          unver == ["https://ex.com/live"], unver)
    check("a real 404 is still reported broken",
          broken == ["https://ex.com/dead"], broken)
    check("unverified wording does not claim it is broken",
          "unknown" in (f["link_unverified"][0]["detail"]),
          f["link_unverified"][0]["detail"])

    # Pacing: two requests to one host must be spaced by the delay.
    lim = S.HostLimiter(delay=0.05)
    t0 = S.time.monotonic()
    for _ in range(4):
        lim.wait("https://ex.com/x")
    spent = S.time.monotonic() - t0
    check("host limiter paces repeat requests", spent >= 0.10, round(spent, 3))

    # Pacing is per host: a different host must not inherit the wait.
    lim2 = S.HostLimiter(delay=0.5)
    lim2.wait("https://a.example/1")
    t1 = S.time.monotonic()
    lim2.wait("https://b.example/1")
    other_host = S.time.monotonic() - t1
    check("limiter does not pace a different host", other_host < 0.05,
          round(other_host, 3))

    t2 = S.time.monotonic()
    S.HostLimiter(delay=0).wait("https://a.example/1")
    check("delay 0 disables pacing", S.time.monotonic() - t2 < 0.02)


def test_siteliner_caveat():
    """Only one of the three percentages compares to Siteliner's. Pin both facts."""
    print("\nsiteliner comparability")
    corpus = [page("https://ex.com/s%d/" % i, service_page(i)) for i in range(12)]
    split = S.content_split(corpus)
    check("content_split returns percentages", split and "duplicate_pct" in split)
    check("percentages sum to ~100",
          abs(split["duplicate_pct"] + split["common_pct"]
              + split["unique_pct"] - 100) < 0.5, split)
    check("split states its basis", "removed at parse time" in split.get("basis", ""),
          split.get("basis"))
    # Siteliner also separates chrome, so the duplicate figure IS comparable.
    check("duplicate % is flagged comparable",
          split.get("duplicate_pct_comparable_to_siteliner") is True, split)
    check("unique % is flagged NOT comparable",
          split.get("unique_pct_comparable_to_siteliner") is False, split)
    check("default threshold matches Screaming Frog's 90%",
          S.DUPE_RATIO == 0.90, S.DUPE_RATIO)


def main():
    print("site_scan check controls")
    test_parser()
    test_url_normalising()
    test_duplicates()
    test_findings()
    test_csv_columns()
    test_import_surface()
    test_blog_segmentation()
    test_indexability_and_exact()
    test_topic_overlap()
    test_similarity_map()
    test_throttle_retry()
    test_siteliner_caveat()
    print("")
    if FAILS:
        print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("All controls passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
