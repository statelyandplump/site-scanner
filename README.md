# Site Scanner

Scans a website for the things people keep asking about: missing alt text,
oversized images, broken links, duplicate content, and title / heading /
description problems.

Produces an HTML report you can open in any browser and send to anyone, plus a
CSV if you want to filter or sort it yourself.

## Running it

**Double-click `SiteScanner.exe`.** Type the website address, press Scan. When it
finishes, the report opens in your browser by itself and is saved to
`Desktop\Site Scans`.

No install, no Python, no command line, no API keys.

### First run

The app is not code-signed, so each operating system warns once:

- **Windows** — "Windows protected your PC". Click **More info** → **Run anyway**.
- **macOS** — **right-click the app and choose Open** (double-clicking is
  blocked). Confirm once.

Both warnings are about the missing paid certificate, not the app. Signing costs
$99/yr (Apple) and roughly $200–400/yr (Windows) and is only worth it if this
goes outside the team.

### Building and sharing it

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name SiteScanner scanner_gui.py
```

That produces one ~11 MB file in `dist/`, for the platform you built on.

**PyInstaller cannot cross-compile** — a Mac build has to run on a Mac. Rather
than finding a Mac, use [`build-release.yml`](build-release.yml): copy it to
`.github/workflows/` in a GitHub repo, push a tag, and GitHub builds Windows and
macOS on its own runners, runs the tests, and publishes both to a Release page
people download from. Free, and nobody needs Python installed.

### From a terminal, if you prefer

```bash
python scanner_gui.py        # same window, run from source
python site_scan.py example.com    # no window, prints to the terminal
```

That crawls every page in the site's sitemap. On a 200-page practice site expect
three to five minutes.

The window's options and the CLI flags below are the same settings.

Useful flags:

```bash
python site_scan.py example.com --limit 25       # just the first 25 pages, for a quick look
python site_scan.py example.com --no-external    # skip outbound links, roughly twice as fast
python site_scan.py example.com --image-kb 150   # stricter image size budget (default 200 KB)
python site_scan.py example.com --delay 0.5      # slower, for a site that rate-limits us
python site_scan.py example.com --workers 4      # gentler on small or slow hosts
python site_scan.py example.com --out reports/   # write the report somewhere specific
```

The report lands in the current folder as `scan-<domain>-<date>.html`.

## Using it from other code

The same scan is importable, so a reporting pipeline can run it without a
subprocess while the CLI above keeps working exactly as it does now:

```python
from site_scan import scan
evidence = scan("example.com").evidence()
```

`scan()` prints nothing, writes nothing, and never calls `sys.exit`. It raises
`NoSitemap` rather than returning an exit code, so a caller can fall back to its
own URL inventory. `evidence()` returns facts only — counts and lists, no
severity and no pass/fail — because the thresholds that decide whether 42 long
titles is a problem belong to the report that consumes this, not to a crawler.

See [`example_import.py`](example_import.py) for a runnable version.

## Reading the report

Findings are grouped by severity. **High** sections are open when you load the
page; everything else is collapsed until you click it.

**High — fix these**

| Finding | What it means |
|---|---|
| Pages that would not load | In the sitemap, but the server never answered. Usually a stale sitemap entry. |
| Pages returning an error | In the sitemap, serving a 404 or 500. Fix the page or drop it from the sitemap. |
| Broken links | The link goes somewhere dead. The URL shown is the *destination*; the detail says which page links to it. |
| Broken images | The image file is missing. |
| Images with no alt attribute | Add descriptive alt text. Counted once per image, not once per page it appears on. |
| Identical page content | Byte-identical body text. Consolidate or redirect one. |
| Near-duplicate page content | 90% or more similar after nav and footer are stripped — the same threshold Screaming Frog uses. Usually templated city pages. |
| Missing title tags | Self-explanatory. |

**Medium** covers duplicate titles and descriptions, missing H1s, missing meta
descriptions, images over 1 MB, and **links that could not be checked** — read
that last one carefully: it means the server rate-limited us and the link's real
status is unknown. It is *not* a list of broken links. If it has entries, re-run
with a longer `--delay`. **Low** is the long tail: title and
description lengths, empty alt text, internal links pointing at a redirect, thin
pages.

Three things worth knowing when you read it:

- **Blog posts are counted separately.** Title and description findings show
  "115 · 73 on blog". The blog number is almost always the platform appending
  the site name to every post, which is one template decision rather than 73
  problems. The non-blog figure is the one to act on.

- **`alt=""` is not a bug.** An empty alt attribute is the correct way to mark a
  decorative image. The scanner separates "no alt attribute at all" (a real
  problem, listed as High) from "empty alt" (listed as Low, and often correct).
  Images with names like `spacer` or `divider` are not flagged at all.
- **Outbound link results vary slightly between runs.** Third-party sites time
  out and drop TLS connections at random, and some servers answer the same URL
  inconsistently. Internal links are stable; treat a one-off external failure as
  worth re-checking rather than as fact.
- **Duplicate content ignores your nav and footer.** Every site would otherwise
  look 70% duplicated. Phrases appearing on nearly every page are treated as
  chrome and subtracted before pages are compared.

## What it does not do

**It does not run JavaScript.** It reads the HTML the server sends. That is fine
for WordPress, Squarespace, and most practice sites, where the content is in the
source. A site that builds its page in the browser will under-report — the scan
will look cleaner than the site is. The report says this in its footer.

It is also not a Screaming Frog replacement. It answers a fixed set of questions
well. SF is still the tool for open-ended crawling.

## If something looks wrong

Run the controls:

```bash
python test_checks.py
python test_gui.py
```

`test_checks.py` feeds known-bad HTML through the same code the scanner uses and
asserts each check fires — and, just as importantly, that a clean page raises
nothing. `test_gui.py` builds the real window and pushes fake worker events
through it; add `--live` to drive an actual scan through the thread and queue.

All should pass. If they do and a finding still looks wrong, check the URL by
hand before dismissing it. On a real site the finding that looked most like a
false positive — every single blog category link returning 404 — turned out to
be real: the platform was writing `+` instead of `%20` in the URL path.
