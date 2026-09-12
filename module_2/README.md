# Module 2 - Web Scraping: Grad Cafe Admissions Data

**Name:** Mike Sasso (JHED: msasso1)
**Course:** JHU EN.605.256 Modern Software Concepts in Python
**Module:** Module 2 - Web Scraping Assignment ("Gathering and Cleaning Grad Cafe Data")
**Due:** September 13, 2026

---

## 1. What is in this folder

```
module_2/
├── README.md                       # this file
├── requirements.txt                # exact package versions (pip freeze)
├── scrape.py                       # scraping logic: GradCafeScraper, scrape_data(), save_data(), load_data()
├── clean.py                        # cleaning logic: clean_data() and its private helpers
├── screenshot.jpg                  # evidence that robots.txt was checked before scraping
├── applicant_data.json             # >= 30,000 cleaned applicant records (output of clean.py)
├── llm_extend_applicant_data.json  # applicant_data.json + llm-generated-program / llm-generated-university
├── llm_hosting/                    # instructor-provided local-LLM standardizer (app.py, canon lists, ...)
└── data/
    ├── robots.txt                  # copy of https://www.thegradcafe.com/robots.txt saved by scrape.py
    ├── raw_entries.json.gz         # raw listing text + page JSON for every scraped entry (input to clean.py)
    ├── raw_entries.json            # (git-ignored, ~50 MB) the same file uncompressed
    ├── raw_html/                   # (git-ignored) every fetched listing page, for offline re-parsing
    ├── raw_entries.jsonl           # (git-ignored) append-only progress log written page by page
    ├── checkpoint.json             # (git-ignored) resume point for an interrupted run
    └── scrape.log                  # (git-ignored) run log
```

## 2. Setup and how to run

Requires **Python 3.10 or newer** (developed and tested on 3.11.13 and 3.14.4).

```bash
cd module_2
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2.1 Scrape (writes data/raw_entries.json and applicant_data.json)

```bash
python scrape.py                   # 30,000 entries, 2 s between requests (~100 min)
```

Useful options:

| Option | Meaning |
| --- | --- |
| `--target N` | stop after at least N entries (default 30000) |
| `--delay S` | seconds to sleep between page requests (default 2.0) |
| `--max-pages N` | fetch at most N pages in this run (handy for a quick test) |
| `--fresh` | ignore the checkpoint and start again from the newest page |
| `--reparse-cache` | no network: rebuild the raw entries from `data/raw_html/` |
| `--no-clean` | skip the cleaning step at the end |

The run is resumable. Every page is appended to `data/raw_entries.jsonl` and
the "next page" cursor is written to `data/checkpoint.json`, so if the run is
interrupted (Ctrl-C, network drop, or the site saying no) simply run
`python scrape.py` again and it continues where it stopped.

### 2.2 Clean (raw entries -> applicant_data.json)

`scrape.py` already calls the cleaner when it finishes, but it can be run on
its own, for example after editing `clean.py`:

```bash
python clean.py                    # data/raw_entries.json (or .json.gz) -> applicant_data.json
python clean.py --input data/raw_entries.json.gz --output applicant_data.json
```

`load_data()` / `save_data()` read and write either plain `.json` or `.json.gz`
by file extension, so the committed `data/raw_entries.json.gz` is enough to
regenerate `applicant_data.json` without scraping again.

### 2.3 LLM standardization (applicant_data.json -> llm_extend_applicant_data.json)

```bash
cd llm_hosting
pip install -r requirements.txt
python app.py --file ../applicant_data.json > ../llm_extend_applicant_data.json
```

See section 7 for what was changed in `llm_hosting/` and what the output looks like.

## 3. robots.txt compliance

`https://www.thegradcafe.com/robots.txt` was checked **before** any scraping,
both by hand (see `screenshot.jpg`, a headless-Chrome capture of the file
taken on 2026-09-11) and programmatically on every run:

1. `GradCafeScraper.check_robots()` downloads robots.txt with `urllib.request`
   and saves a copy to `data/robots.txt`.
2. The text is fed to `urllib.robotparser.RobotFileParser` and
   `can_fetch()` is asked about the listing (`/survey/`), a result page
   (`/result/<id>`) and, as a sanity check, a disallowed path (`/signin`).
3. Python's parser only keeps the **first** `User-agent: *` group, and Grad
   Cafe's file has two of them (one with `Allow: /`, one with the `Disallow`
   lines), so `scrape.py` also evaluates the file with RFC 9309 semantics in
   `_robots_allows()`: all applicable groups are merged and the longest
   matching path wins. The scraper only proceeds when **both** checks allow
   the survey and result pages, and `_assert_allowed()` re-checks every URL
   right before it is fetched.
4. A `Crawl-delay` directive would automatically raise the request delay
   (there is none today).

What the file says for a generic crawler (`User-agent: *`): `Allow: /`, with
`/signin`, `/register`, `/forgot-password`, `/reset-password`,
`/confirm-password`, `/verify-email` and `/profile` disallowed. The survey
listing and the public result pages are therefore permitted, and none of the
disallowed (login / account) paths are ever requested. The file also names a
number of AI crawlers (GPTBot, ClaudeBot, CCBot, ...) that are disallowed
entirely; this scraper is none of them and identifies itself honestly as
`JHU-EN605256-GradCafeScraper` with a contact address in its User-Agent. The
`Content-Signal: ai-train=no` line forbids using the content to train AI
models, which this coursework does not do.

**Politeness.** One request at a time, a fixed 2-second sleep between page
requests (about 0.3 requests per second), a small and slow retry budget for
transient failures only (one retry after 30 s for a 5xx response; retries
after 30 s and then 120 s for a timeout or connection error), and an
immediate, unconditional stop on HTTP 401 / 403 / 429 or a Cloudflare
challenge page. Nothing is done to work around a block: the run ends (exit
code 2 when the site rejects a request, 3 when the network keeps failing),
progress is saved, and it can be resumed later. Only the public listing pages
are fetched (no login-protected pages, no per-applicant detail pages).

## 4. Scraping approach (scrape.py)

**Workflow used: urllib-only.** Selenium was *not* needed. Grad Cafe returns
fully server-rendered HTML for the listing page, and the only thing that
blocks a plain Python request is Cloudflare rejecting the default
`Python-urllib/3.x` User-Agent with HTTP 403. A descriptive, honest User-Agent
string (`Mozilla/5.0 (compatible; JHU-EN605256-GradCafeScraper/1.0; student
coursework; contact msasso1@jh.edu)`) is served normally, so the whole
pipeline is `urllib.request` + `urllib.parse` + `urllib.robotparser` for
fetching and URL handling, and BeautifulSoup + `re` + `str` methods for
parsing. No browser or driver is required to reproduce the data.

Everything lives in the class `GradCafeScraper`:

| Method | Role |
| --- | --- |
| `check_robots()` | fetch/parse/save robots.txt, verify access (section 3) |
| `scrape_data(resume=True)` | main loop: fetch page -> parse -> persist -> follow "Next" -> sleep |
| `reparse_cached_pages()` | rebuild the raw entries from `data/raw_html/` without the network |
| `_build_start_url()` | builds `https://www.thegradcafe.com/survey/?page=1` with `urlunparse`/`urlencode` |
| `_next_page_url(soup, page_url)` | finds the "Next" anchor inside `<nav aria-label="Results pagination">`, resolves it with `urljoin`, and refuses off-site hosts |
| `_describe_cursor(url)` | `parse_qs` + base64 to decode the pagination cursor for readable log lines |
| `_assert_allowed(url)` | robots + same-host guard run before every request |
| `_http_get(url)` / `_fetch_page(url)` | the single GET, block detection, and the one-retry policy |
| `_parse_page(html, page_url)` | groups table rows into entries and returns them plus the next URL |
| `_parse_entry(main_row, extra_rows, page_url)` | pulls the visible text of one applicant row group |
| `_extract_listing_json(soup)` | reads the JSON copy of the page's entries from `<div id="app" data-page="...">` |
| `_load_checkpoint` / `_save_checkpoint` / `_append_jsonl` / `_load_jsonl` | resume support |
| `_cache_page_html(html, n, url)` | stores each fetched page under `data/raw_html/page_NNNNN.html` |

Module-level `save_data(entries, path)` and `load_data(path)` write/read the
JSON files (atomic write via a temporary file), and `clean.py` imports them.

**Pagination.** Grad Cafe no longer uses simple `?page=N` pagination:
`?page=2` returns exactly the same 20 newest rows as `?page=1`. The real next
page is the "Next" link at the bottom of the table, whose URL carries a
base64-encoded cursor such as
`{"created_at":"2026-08-28 16:54:05","admitid":1020462,...}`. The scraper
therefore reads the next URL from each page instead of computing it, which is
also stable against new entries being posted while the scrape runs. Entries
are de-duplicated by result id in any case.

**Page structure.** Each listing page holds 20 applicants in one `<table>`.
An applicant is a main `<tr>` with five cells (school; program + degree as two
`<span>`s; date added; decision badge; the `/result/<id>` link) optionally
followed by a `<tr class="tw-border-none">` with badge `<div>`s (start term,
International/American, GPA, GRE, GRE V, GRE AW) and another with a `<p>`
holding the applicant's comment. `_parse_page()` walks the rows in order,
starting a new entry at every row that has a `/result/<id>` link and
attaching the following badge/comment rows to it. The badge that duplicates
the decision for narrow screens (`md:tw-hidden`) is skipped so the decision is
recorded once. Text is taken with `get_text(" ", strip=True)` and whitespace is
collapsed, which also drops the `<!-- -->` comment nodes the site inserts
inside badges ("GPA <!-- -->3.40").

**The page's own JSON.** The listing is an Inertia.js page: the root
`<div id="app" data-page="...">` attribute holds a JSON document whose
`props.results.data` list is the server's copy of the same 20 entries the table
renders (`id`, `school`, `program`, `level`, `decision`,
`date_of_notification`, `created_at`, `notes`, `status`, `season`, `ugpa`,
`greq`, `grev`, `grew`, ...). `_extract_listing_json()` reads that attribute
with BeautifulSoup and `json.loads`, and each raw entry keeps its record under
`listing_json`. The visible HTML stays the primary source; the JSON is used
because it carries the decision date **with its year**, which the badge does
not show, and it lets `clean.py` fill a field if a badge is ever missing. A
page without the payload still parses (the key is simply `null`).

**Raw entry format** (`data/raw_entries.json[.gz]`): the visible text exactly
as listed, nothing interpreted yet, plus the page's JSON record.

```json
{
  "result_id": 1020479,
  "url": "https://www.thegradcafe.com/result/1020479",
  "school_text": "Bangladesh University of Engineering and Technology (BUET)",
  "program_text": "Electrical Engineering and Computer Science",
  "degree_text": "PhD",
  "date_added_text": "Sep 10, 2026",
  "decision_text": "Wait listed on Sep 10",
  "tags_text": ["Spring 2027", "International", "GRE 163", "GRE V 158", "GRE AW 4.00", "GPA 3.57"],
  "comment_text": null,
  "listing_json": {"id": 1020479, "school": "...", "date_of_notification": "2026-09-10T00:00:00.000000Z", "ugpa": "3.57", "greq": "163", "...": "..."},
  "source_page_url": "https://www.thegradcafe.com/survey/?page=1",
  "scraped_at": "2026-09-12T06:46:54+00:00"
}
```

## 5. Cleaning approach (clean.py)

`clean_data(raw_entries)` turns each raw entry into a typed record. Every raw
string is carried along untouched under the `raw` key, so any cleaned value
can be traced back to what the website showed; nothing applicant-provided is
altered, only parsed. Missing or unavailable values are always `null`.

| Key | Type | Source / rule |
| --- | --- | --- |
| `result_id` | int | number in the `/result/<id>` link |
| `url` | str | absolute link to the applicant entry |
| `program` | str | `"<program>, <university>"` - the legacy Grad Cafe listing form that the LLM standardizer expects |
| `program_name` | str | program `<span>` text |
| `university` | str | school cell text |
| `degree` | str | second `<span>` (Masters, PhD, MFA, JD, Other, ...) |
| `date_added` | str `YYYY-MM-DD` | "Sep 11, 2026" parsed with `datetime.strptime` |
| `status` | str | `Accepted`, `Rejected`, `Waitlisted` (site shows "Wait listed"), `Interview`, `Other` |
| `decision_date` | str `YYYY-MM-DD` | full date from the page JSON (`date_of_notification`); otherwise month/day from the badge with the year inferred (see below) |
| `decision_date_source` | str | `site_json` or `badge_year_inferred` (null when there is no date) |
| `term` | str | "Fall 2026", "Spring 2027", ... from the badge row (JSON `season` as fallback) |
| `us_or_international` | str | `International` or `American` (null when the site records "Other") |
| `gpa` | float | "GPA 3.57" -> 3.57 (kept exactly as reported, even on a 10-point scale) |
| `gre` | int | "GRE 163" -> 163. The site's JSON stores this badge as `greq`, i.e. applicants normally enter the Quantitative score here |
| `gre_v` | int | "GRE V 158" -> 158 |
| `gre_aw` | float | "GRE AW 4.00" -> 4.0 |
| `comments` | str | the `<p>` text, entities decoded, tags removed, line breaks kept |
| `other_tags` | list | any badge that matched none of the patterns above (empty so far) |
| `raw` | object | school, program, degree, date_added, decision, tags, comment, date_of_notification, scraped_at |

**Decision date.** The badge shows "Accepted on Sep 09" without a year, so
the year normally comes from the page JSON's `date_of_notification`, which was
checked against four `/result/<id>` detail pages and matched every time. If a
page ever lacks the JSON, `_resolve_decision_date()` falls back to the year of
`date_added`, moving the decision to the previous year when its month/day is
later than the date-added month/day (e.g. "Rejected on Dec 20" on an entry
added Jan 5, 2026); `decision_date_source` records which path was used. On the
pages checked, that fallback would have disagreed with the site's date on
about 1.4% of entries (applicants who typed a decision date a few days after
they posted), which is why the JSON value is preferred. The original badge
text is always kept in `raw.decision`.

Private helpers: `_clean_entry`, `_clean_text` (HTML entity decoding, tag
stripping, whitespace collapse), `_join_program`, `_first_present`,
`_parse_iso_date`, `_parse_date_added`, `_normalize_status`,
`_parse_decision`, `_resolve_decision_date`, `_parse_term`,
`_parse_applicant_type`, `_to_number`, `_to_number_or_none`,
`_classify_tags`. Regular expressions (`TERM_PATTERN`, `GRE_AW_PATTERN`,
`GRE_V_PATTERN`, `GRE_PATTERN`, `GPA_PATTERN`, `DECISION_PATTERN`) do the
matching; the GRE patterns are tried from most to least specific so
"GRE AW 4.00" is never mistaken for "GRE 4".

**Relation to the sample output in the assignment.** The assignment's sample
(scraped from the previous Grad Cafe layout) keeps every value as the raw
listing string. This output keeps the same vocabulary but stores parsed
values, and the raw strings live under `raw`:

| Sample key | Sample value | This file |
| --- | --- | --- |
| `program` | `"Mathematics, University Of British Columbia"` | `program` (same form) plus `program_name` / `university` |
| `comments` | text or `""` | `comments` (text or `null`) |
| `date_added` | `"Added on March 31, 2024"` | `date_added` = `"2024-03-31"`, original in `raw.date_added` |
| `url` | result link | `url` |
| `status` | `"Accepted on 1 Mar"` | `status` = `"Accepted"` and `decision_date` = `"2024-03-01"`, original in `raw.decision` |
| `term` | `"Fall 2024"` | `term` |
| `US/International` | `"American"` | `us_or_international` |
| `GPA` | `"GPA 3.88"` | `gpa` = `3.88`, original in `raw.tags` |
| `GRE`, `GRE V`, `GRE AW` | `"GRE 320"` ... | `gre`, `gre_v`, `gre_aw` as numbers, originals in `raw.tags` |
| `Degree` | `"Masters"` | `degree` |

Example cleaned record:

```json
{
  "result_id": 1020479,
  "url": "https://www.thegradcafe.com/result/1020479",
  "program": "Electrical Engineering and Computer Science, Bangladesh University of Engineering and Technology (BUET)",
  "program_name": "Electrical Engineering and Computer Science",
  "university": "Bangladesh University of Engineering and Technology (BUET)",
  "degree": "PhD",
  "date_added": "2026-09-10",
  "status": "Waitlisted",
  "decision_date": "2026-09-10",
  "decision_date_source": "site_json",
  "term": "Spring 2027",
  "us_or_international": "International",
  "gpa": 3.57,
  "gre": 163,
  "gre_v": 158,
  "gre_aw": 4.0,
  "comments": null,
  "other_tags": [],
  "raw": { "...": "original listing text" }
}
```

## 6. Run statistics

TBD_RUN_STATS

## 7. LLM standardization (llm_hosting)

TBD_LLM_SECTION

## 8. Known bugs, limitations and edge cases

No known bugs in the delivered pipeline; the points below are the edge cases
and limitations found while building and checking it.

- **Decision-date year.** Only the page JSON carries the decision year. When it
  is present (every page fetched so far) `decision_date` is exact. If a page
  ever lacked the payload, the badge fallback would infer the year and mark the
  record `decision_date_source = "badge_year_inferred"`; on the pages checked
  that fallback disagrees with the site for about 1.4% of entries, so
  downstream analysis should prefer records with `site_json`.
- **Decisions without a date.** A badge that reads just "Other" (and, on the
  old layout, a bare "Accepted") has no date: `status` is set and
  `decision_date` is `null`.
- **Cloudflare e-mail obfuscation.** When an applicant types an e-mail address
  into a comment, the HTML shows it as `[email protected]` while the page JSON
  holds the real address. `comments` uses the visible HTML text (so it does
  not harvest addresses); the JSON copy is in `data/raw_entries.json.gz`.
- **Nationality "Other".** Entries whose site record says `status = "Other"`
  show no International/American badge; `us_or_international` is `null`.
- **GPA scales.** GPA is kept exactly as reported. Most values are on a 4.0
  scale but some applicants report other scales (or typos such as 0.1); no
  re-scaling is attempted because the scale is not stated on the site.
- **GRE badge meaning.** The badge labelled "GRE" is stored by the site as
  `greq` and its values (139-170 in the data) are Quantitative section
  scores, not the 260-340 total, so `gre` should be read as GRE Quantitative.
- **Comments are single paragraphs.** The listing renders one `<p>` per
  comment and no line breaks were observed; newlines are preserved anyway if
  they ever appear.
- **Site instability.** During the run Grad Cafe answered some requests with
  read timeouts and HTTP 502. The scraper waits 30 s / 120 s and retries at
  most twice for those, stops (keeping its checkpoint) if that is not enough,
  and never retries a 401/403/429.
- **Duplicates across runs.** Cursor pagination is stable, but entries are
  still de-duplicated by result id when resuming and when re-parsing the
  cache, so a page that is fetched twice cannot produce two records.
- **Detail pages are not fetched.** Fields that exist only on
  `/result/<id>` pages (notification method, institution statistics) are not
  collected; the JSON in the listing already includes the decision date, so
  the extra 30,000 requests were not justified.
