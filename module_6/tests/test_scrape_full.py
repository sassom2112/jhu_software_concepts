"""
test_scrape_full.py - The full, resumable scrape and the scrape.py command line.

A full scrape (Module 2's 30,000-entry run) saves progress after every page:
raw_entries.jsonl, checkpoint.json and a copy of each page under raw_html/.
These tests run it against fake_site and then read those files back, to check
that an interrupted run can always be resumed and that every way the run can
stop -- target reached, page limit, empty page, blocked, network down, Ctrl+C
-- leaves the files in a state the next run understands.
"""

from __future__ import annotations

import json
import runpy
import sys
import urllib.error

import pytest

from worker.etl import scrape
from fake_gradcafe import ROBOTS_URL, applicant, http_error, listing_page, page, survey_url
from worker.etl.scrape import GradCafeScraper, load_data

pytestmark = pytest.mark.db


def result_ids(entries):
    return [entry["result_id"] for entry in entries]


def read_checkpoint(folder):
    return json.loads((folder / "checkpoint.json").read_text(encoding="utf-8"))


def read_progress_log(folder):
    lines = (folder / "raw_entries.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["result_id"] for line in lines]


# --------------------------------------------------------------------------- #
#                          scrape_data(): a full run                          #
# --------------------------------------------------------------------------- #

def test_full_scrape_saves_progress_after_every_page(fake_site, scraper, tmp_path):
    fake_site.serve_listing([[105, 104], [103, 102]])

    entries = scraper.scrape_data(resume=False)

    assert result_ids(entries) == [105, 104, 103, 102]
    assert read_progress_log(tmp_path) == [105, 104, 103, 102]
    checkpoint = read_checkpoint(tmp_path)
    assert (checkpoint["next_url"], checkpoint["finished"], checkpoint["pages_fetched"]) == (None, True, 2)
    assert sorted(p.name for p in (tmp_path / "raw_html").iterdir()) == ["page_00001.html", "page_00002.html"]
    assert fake_site.sleeps == [2.0]


def test_full_scrape_stops_at_the_target(fake_site, tmp_path):
    urls = fake_site.serve_listing([[105, 104], [103, 102]])

    entries = GradCafeScraper(data_dir=tmp_path, target_entries=2).scrape_data()

    assert result_ids(entries) == [105, 104]
    assert urls[1] not in fake_site.requested
    assert fake_site.sleeps == []                                             # no pause once the target is met
    assert read_checkpoint(tmp_path)["finished"] is True


def test_a_page_limit_ends_the_run_and_the_next_run_resumes(fake_site, tmp_path):
    urls = fake_site.serve_listing([[105, 104], [103, 102]])

    GradCafeScraper(data_dir=tmp_path, max_pages=1).scrape_data()
    checkpoint = read_checkpoint(tmp_path)
    resumed = GradCafeScraper(data_dir=tmp_path)
    entries = resumed.scrape_data()

    assert (checkpoint["next_url"], checkpoint["finished"]) == (urls[1], False)
    assert result_ids(entries) == [105, 104, 103, 102]
    assert resumed.pages_fetched == 2
    assert fake_site.requested.count(urls[0]) == 1                            # page 1 was not fetched again


def test_resume_skips_blank_and_damaged_lines_in_the_progress_log(fake_site, scraper, tmp_path):
    progress = '{"result_id": 105}\n\n{"result_id": 104, "trunc\n{"result_id": 103}\n'   # a crash cut line 3
    (tmp_path / "raw_entries.jsonl").write_text(progress, encoding="utf-8")
    (tmp_path / "checkpoint.json").write_text('{"next_url": null, "pages_fetched": 7}', encoding="utf-8")

    entries = scraper.scrape_data()

    assert result_ids(entries) == [105, 103]                                  # the entry AFTER the damage survives
    assert scraper.pages_fetched == 7
    assert fake_site.requested == []                                          # the listing was already exhausted


def test_resume_with_a_checkpoint_but_no_progress_log_yet(fake_site, scraper, tmp_path):
    (tmp_path / "checkpoint.json").write_text('{"next_url": null}', encoding="utf-8")

    assert scraper.scrape_data() == []
    assert fake_site.requested == []


def test_resume_when_the_target_was_already_reached_fetches_nothing(fake_site, tmp_path):
    (tmp_path / "raw_entries.jsonl").write_text('{"result_id": 105}\n{"result_id": 104}\n', encoding="utf-8")
    checkpoint = {"next_url": survey_url(), "pages_fetched": 1, "finished": True}
    (tmp_path / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")

    entries = GradCafeScraper(data_dir=tmp_path, target_entries=2).scrape_data()

    assert result_ids(entries) == [105, 104]
    assert fake_site.requested == []


@pytest.mark.parametrize("checkpoint_text", ["{damaged", "[1, 2]"], ids=["not-json", "not-an-object"])
def test_a_progress_log_without_a_usable_checkpoint_is_an_error(fake_site, scraper, tmp_path, checkpoint_text):
    (tmp_path / "raw_entries.jsonl").write_text('{"result_id": 105}\n', encoding="utf-8")
    (tmp_path / "checkpoint.json").write_text(checkpoint_text, encoding="utf-8")

    with pytest.raises(scrape.ScrapeStateError):
        scraper.scrape_data()                                                 # the user has to decide, not the code

    assert fake_site.requested == []


def test_a_fresh_run_discards_earlier_progress(fake_site, scraper, tmp_path):
    fake_site.serve_listing([[105, 104]])
    (tmp_path / "raw_entries.jsonl").write_text('{"result_id": 1}\n', encoding="utf-8")
    (tmp_path / "checkpoint.json").write_text("{}", encoding="utf-8")
    (tmp_path / "raw_html").mkdir()
    (tmp_path / "raw_html" / "page_00009.html").write_text("old page", encoding="utf-8")

    entries = scraper.scrape_data(resume=False)

    assert result_ids(entries) == [105, 104]
    assert read_progress_log(tmp_path) == [105, 104]                          # entry 1 is gone from the log too
    assert [p.name for p in (tmp_path / "raw_html").iterdir()] == ["page_00001.html"]


def test_an_empty_page_stops_the_run(fake_site, scraper):
    urls = fake_site.serve_listing([[105], [], [104]])

    assert result_ids(scraper.scrape_data(resume=False)) == [105]
    assert urls[2] not in fake_site.requested                                 # it did not follow the empty page


def test_a_next_link_back_to_the_same_page_stops_the_run(fake_site, scraper):
    fake_site.serve_listing([[105]])
    fake_site.serve(survey_url(), page(listing_page([applicant(105)], next_url=survey_url())))

    assert result_ids(scraper.scrape_data(resume=False)) == [105]
    assert fake_site.requested.count(survey_url()) == 1


def test_three_pages_with_nothing_new_stop_the_run(fake_site, scraper):
    urls = fake_site.serve_listing([[105], [105], [105], [105], [104]])

    assert result_ids(scraper.scrape_data(resume=False)) == [105]
    assert fake_site.requested == [ROBOTS_URL] + urls[:4]                     # 1 new page, then exactly 3 stale


def test_raw_json_snapshot_is_written_every_n_pages(fake_site, scraper, tmp_path, monkeypatch):
    monkeypatch.setattr(scrape, "SAVE_JSON_EVERY_N_PAGES", 1)
    fake_site.serve_listing([[105, 104]])

    scraper.scrape_data(resume=False)

    assert result_ids(load_data(tmp_path / "raw_entries.json")) == [105, 104]


def test_pages_are_not_cached_when_caching_is_off(fake_site, tmp_path):
    fake_site.serve_listing([[105]])

    GradCafeScraper(data_dir=tmp_path, cache_html=False).scrape_data(resume=False)

    assert not (tmp_path / "raw_html").exists()


def test_full_scrape_refuses_when_robots_disallows(fake_site, scraper):
    fake_site.serve(ROBOTS_URL, page("User-agent: *\nDisallow: /\n"))

    with pytest.raises(PermissionError):
        scraper.scrape_data()


STOPS = pytest.mark.parametrize(
    ("failure", "error"),
    [
        (http_error(403), scrape.ScrapeBlockedError),
        (urllib.error.URLError("network down"), scrape.ScrapeNetworkError),
        (KeyboardInterrupt(), KeyboardInterrupt),
    ],
    ids=["blocked", "network-down", "ctrl-c"],
)


@STOPS
def test_a_stop_mid_run_keeps_progress_and_points_at_the_failed_page(fake_site, scraper, tmp_path, failure, error):
    urls = fake_site.serve_listing([[105, 104], [103, 102]])
    fake_site.serve(urls[1], failure)

    with pytest.raises(error):
        scraper.scrape_data(resume=False)

    assert result_ids(scraper.entries) == [105, 104]
    checkpoint = read_checkpoint(tmp_path)
    assert (checkpoint["next_url"], checkpoint["finished"]) == (urls[1], False)


@STOPS
def test_a_stop_on_the_very_first_page_still_writes_a_checkpoint(fake_site, scraper, tmp_path, failure, error):
    fake_site.serve_listing([[105]])
    fake_site.serve(survey_url(), failure)

    with pytest.raises(error):
        scraper.scrape_data(resume=False)

    checkpoint = read_checkpoint(tmp_path)                                    # no page ever finished, yet it exists
    assert (checkpoint["next_url"], checkpoint["finished"]) == (survey_url(), False)


# --------------------------------------------------------------------------- #
#                   Re-parsing the saved pages, offline                       #
# --------------------------------------------------------------------------- #

def test_pages_cached_by_a_scrape_can_be_reparsed_later(fake_site, scraper, tmp_path):
    urls = fake_site.serve_listing([[105, 104], [103]])
    scraper.scrape_data(resume=False)
    requests_during_scrape = len(fake_site.requested)

    reparsed = GradCafeScraper(data_dir=tmp_path).reparse_cached_pages()

    assert [(e["result_id"], e["source_page_url"]) for e in reparsed] == [
        (105, urls[0]), (104, urls[0]), (103, urls[1]),
    ]
    assert len(fake_site.requested) == requests_during_scrape                 # re-parsing fetched nothing


def test_reparse_reads_old_cache_files_without_a_header(fake_site, scraper, tmp_path):
    cache = tmp_path / "raw_html"
    cache.mkdir()
    header = f"<!-- {scrape.PRODUCT_TOKEN} source-url: {survey_url()} fetched-at: 2026-09-20T12:00:00+00:00 -->\n"
    (cache / "page_00001.html").write_text(header + listing_page([applicant(105), applicant(104)]), encoding="utf-8")
    (cache / "page_00002.html").write_text(listing_page([applicant(104), applicant(103)]), encoding="utf-8")  # older file: no header

    entries = scraper.reparse_cached_pages()

    assert result_ids(entries) == [105, 104, 103]
    assert (entries[0]["source_page_url"], entries[0]["scraped_at"]) == (survey_url(), "2026-09-20T12:00:00+00:00")
    assert entries[2]["source_page_url"].startswith("file://")                # falls back to the file itself
    assert fake_site.requested == []


def test_reparse_without_cached_pages_is_an_error(scraper):
    with pytest.raises(scrape.ScrapeStateError):
        scraper.reparse_cached_pages()


# --------------------------------------------------------------------------- #
#                  Command line: python -m worker.etl.scrape                  #
# --------------------------------------------------------------------------- #

@pytest.fixture
def module_dir(tmp_path, monkeypatch):
    """Run scrape.py's command line inside a temporary folder instead of module_6/."""
    monkeypatch.setattr(scrape, "HERE", tmp_path)
    return tmp_path


def test_main_scrapes_saves_and_cleans(fake_site, module_dir):
    fake_site.serve_listing([[105, 104]])

    assert scrape.main(["--fresh"]) == 0

    assert result_ids(load_data(module_dir / "data" / "raw_entries.json")) == [105, 104]
    assert result_ids(load_data(module_dir / "data" / "raw_entries.json.gz")) == [105, 104]
    cleaned = load_data(module_dir / "applicant_data.json")
    assert cleaned[0]["program"] == "Computer Science, Johns Hopkins University"


def test_main_options_for_names_cleaning_and_caching(fake_site, module_dir):
    fake_site.serve_listing([[105], [104]])

    exit_code = scrape.main(["--data-dir", "../cache", "--raw-output", "raw.json", "--no-clean",
                             "--no-cache-html", "--max-pages", "1", "--target", "10", "--delay", "0.5"])

    assert exit_code == 0
    assert result_ids(load_data(module_dir / "cache" / "raw.json")) == [105]  # "../cache" was kept inside
    assert not (module_dir / "applicant_data.json").exists()
    assert not (module_dir / "cache" / "raw_html").exists()
    assert fake_site.sleeps == [0.5]


def test_main_can_reparse_the_cache_without_the_network(fake_site, module_dir):
    cache = module_dir / "data" / "raw_html"
    cache.mkdir(parents=True)
    (cache / "page_00001.html").write_text(listing_page([applicant(105)]), encoding="utf-8")

    assert scrape.main(["--reparse-cache", "--no-clean"]) == 0
    assert result_ids(load_data(module_dir / "data" / "raw_entries.json")) == [105]
    assert fake_site.requested == []


@pytest.mark.parametrize(
    ("failure", "exit_code"),
    [
        (http_error(403), 2),                                                 # the site refused
        (urllib.error.URLError("network down"), 3),                          # kept failing after two retries
        (http_error(404), 4),                                                 # an unexpected HTTP status
        (KeyboardInterrupt(), 130),                                           # Ctrl+C
    ],
    ids=["blocked", "network", "http-404", "ctrl-c"],
)
def test_main_exit_codes_still_save_what_was_collected(fake_site, module_dir, failure, exit_code):
    urls = fake_site.serve_listing([[105, 104], [103, 102]])
    fake_site.serve(urls[1], failure)

    assert scrape.main(["--fresh"]) == exit_code
    assert result_ids(load_data(module_dir / "data" / "raw_entries.json")) == [105, 104]


def test_main_saves_nothing_when_robots_disallows(fake_site, module_dir):
    fake_site.serve(ROBOTS_URL, page("User-agent: *\nDisallow: /\n"))

    assert scrape.main([]) == 2
    assert not (module_dir / "data" / "raw_entries.json").exists()


def test_main_reparse_without_a_cache_exits_5(fake_site, module_dir):
    assert scrape.main(["--reparse-cache"]) == 5


def test_main_refuses_a_name_with_nothing_usable_in_it(module_dir, capsys):
    # A path is reduced to its last part ("../cache" -> "cache"); ".." has no usable last part at all.
    assert scrape.main(["--data-dir", ".."]) == 1
    assert "error:" in capsys.readouterr().err


def test_running_scrape_py_exits_with_mains_code(fake_site, monkeypatch):
    # runpy re-runs scrape.py from scratch, so HERE is the real module_6/ folder again: keep to
    # an argument that is refused before anything is written, and keep fake_site as a safety net.
    monkeypatch.setattr(sys, "argv", ["scrape.py", "--data-dir", ".."])

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("worker.etl.scrape", run_name="__main__")

    assert stopped.value.code == 1
    assert fake_site.requested == []