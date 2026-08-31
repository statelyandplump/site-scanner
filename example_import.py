#!/usr/bin/env python3
"""
Using the scanner as a library.

The window and the command line are two front ends over one `scan()` call. This
is the third: import it, get a dict of facts, do what you like with them. No
subprocess, no temp file, no parsing of console output.

    python example_import.py example.com
    python example_import.py example.com 25        # first 25 pages only

Copy `collect()` into whatever builds your report.
"""

import json
import sys

from site_scan import NoSitemap, Options, scan


def collect(domain, limit=None):
    """Return one site's scan as a plain dict."""
    opts = Options(
        limit=limit,
        workers=6,
        external=True,      # check outbound links too; slower
        # image_kb / banner_kb default to 100 / 200. Set them here if your
        # performance budget differs.
    )
    try:
        result = scan(domain, opts)
    except NoSitemap:
        # Worth distinguishing from a failed scan: having no sitemap at all is
        # itself a finding, not an error.
        return {"source": "site-scanner", "error": "no_sitemap", "site": domain}
    return result.evidence()


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2

    ev = collect(sys.argv[1], limit=int(sys.argv[2]) if len(sys.argv) > 2 else None)
    if ev.get("error"):
        print("no sitemap found for %s" % ev["site"])
        return 1

    print("site                  : %s" % ev["site"])
    print("urls in sitemap       : %s" % ev["crawl"]["urls_in_sitemap"])
    print("pages parsed          : %s" % ev["crawl"]["pages_parsed"])
    print("images missing alt    : %s" % ev["images"]["missing_alt_count"])
    print("images with alt=''    : %s  (usually correct, not the same finding)"
          % ev["images"]["empty_alt_count"])
    print("images over %skb      : %s" % (ev["images"]["budget_kb"],
                                          ev["images"]["over_budget_count"]))
    print("broken links          : %s  (%s internal, those want a redirect)"
          % (ev["links"]["broken_count"], ev["links"]["broken_internal_count"]))
    print("links unverified      : %s  (rate-limited, status unknown)"
          % ev["links"].get("unverified_count", 0))
    print("identical pages       : %s clusters"
          % ev["content"]["exact_duplicate_cluster_count"])
    print("near-duplicate pages  : %s clusters"
          % ev["content"]["duplicate_cluster_count"])
    if "duplicate_pct" in ev["content"]:
        print("content duplicate     : %s%%  (%s)"
              % (ev["content"]["duplicate_pct"], ev["content"]["basis"]))
    print("")
    print("top-level keys        : %s" % ", ".join(ev.keys()))
    print("size of evidence      : %d bytes" % len(json.dumps(ev)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
