"""
test_watermark.py - The Pull Data task: only entries newer than the watermark, which then moves forward.

handle_scrape_new_data(conn, payload) runs here the way the worker runs it:
inside one transaction on the *_test database.  Only Grad Café is fake:
worker_site serves listing pages, newest result id first, exactly like the
real site, so the real scraper, clean.py and the loader all run.

The watermark is the "gradcafe" row of ingestion_watermarks: last_seen (TEXT)
holds the highest result id already read.  A pull reads it first, fetches only
higher ids, and stores the highest id it saw.
"""

from __future__ import annotations

import logging

import psycopg
import pytest

from db.load_data import count_rows, fetch_applicants, get_watermark, newest_p_id, set_watermark
from db.snapshot import snapshot_status
from fake_gradcafe import ROBOTS_URL, page
from worker.consumer import TaskError, handle_scrape_new_data
from worker.etl import incremental_scraper
from worker.etl.incremental_scraper import NewEntries, SeenUpTo, fetch_new_entries
from worker.etl.ingest import insert_scraped_entries

pytestmark = [pytest.mark.db, pytest.mark.buttons]


def pull(conn, payload=None) -> dict:
    """One scrape_new_data task, in a transaction of its own, as the worker runs it."""
    with conn.transaction():
        return handle_scrape_new_data(conn, payload or {})


def stored_ids(conn) -> list[int]:
    return [row["p_id"] for row in fetch_applicants(conn)]


def store(conn, entry_factory, *result_ids) -> None:
    """Rows that are already in the database before the pull."""
    with conn.transaction():
        insert_scraped_entries(conn, [entry_factory(result_id) for result_id in result_ids])


# --------------------------------------------------------------------------- #
#                  Reading the watermark, then moving it forward               #
# --------------------------------------------------------------------------- #

def test_the_first_pull_starts_after_the_newest_stored_entry(db_conn, worker_site, entry_factory):
    store(db_conn, entry_factory, 100, 101)                  # what the data file loaded
    urls = worker_site.serve_listing([[104, 103], [102, 101], [100]])

    result = pull(db_conn)

    assert get_watermark(db_conn) == "104"                   # the highest id seen, as TEXT
    assert (result["last_seen"], result["fetched"], result["inserted"]) == (101, 3, 3)
    assert stored_ids(db_conn) == [104, 103, 102, 101, 100]
    assert urls[2] not in worker_site.requested              # older pages are never fetched


def test_a_stored_watermark_is_read_first_and_only_newer_entries_are_fetched(db_conn, worker_site):
    set_watermark(db_conn, "102")
    urls = worker_site.serve_listing([[105, 104], [103, 102], [101]])

    result = pull(db_conn)

    assert result["last_seen"] == 102
    assert stored_ids(db_conn) == [105, 104, 103]            # nothing at or below 102
    assert urls[2] not in worker_site.requested
    assert get_watermark(db_conn) == "105"


def test_a_second_pull_inserts_nothing_and_keeps_the_watermark(db_conn, worker_site):
    worker_site.serve_listing([[105, 104], [103]])
    first = pull(db_conn)

    second = pull(db_conn)

    assert first["inserted"] == 3
    assert (second["fetched"], second["inserted"], second["pages"]) == (0, 0, 1)
    assert get_watermark(db_conn) == "105"
    assert count_rows(db_conn) == 3


def test_new_entries_since_the_last_pull_are_the_only_ones_added(db_conn, worker_site):
    worker_site.serve_listing([[103, 102]])
    pull(db_conn)
    worker_site.serve_listing([[106, 105], [104, 103], [102]])   # three newer entries appeared

    result = pull(db_conn)

    assert (result["last_seen"], result["inserted"]) == (103, 3)
    assert stored_ids(db_conn) == [106, 105, 104, 103, 102]
    assert get_watermark(db_conn) == "106"


def test_the_stored_watermark_is_the_maximum_id_of_the_batch_not_the_last_one_read(db_conn, worker_site):
    worker_site.serve_listing([[110, 108], [109]])              # out of order across pages

    pull(db_conn)

    assert get_watermark(db_conn) == "110"


def test_payload_since_overrides_the_watermark_but_never_moves_it_back(db_conn, worker_site, entry_factory):
    store(db_conn, entry_factory, 103, 104, 105)
    set_watermark(db_conn, "105")
    worker_site.serve_listing([[105, 104], [103, 102], [101, 100]])

    result = pull(db_conn, {"since": 101})

    assert (result["last_seen"], result["fetched"], result["inserted"]) == (101, 4, 1)
    assert stored_ids(db_conn) == [105, 104, 103, 102]          # only 102 was missing
    assert get_watermark(db_conn) == "105"                      # not lowered to 101


def test_a_since_above_every_entry_fetches_nothing_and_leaves_the_watermark(db_conn, worker_site):
    set_watermark(db_conn, "105")
    worker_site.serve_listing([[105, 104]])

    result = pull(db_conn, {"since": 2_000_000})

    assert (result["fetched"], result["inserted"]) == (0, 0)
    assert get_watermark(db_conn) == "105"                      # since alone never moves it


def test_an_empty_database_reads_the_whole_listing(db_conn, worker_site):
    worker_site.serve_listing([[3, 2], [1]])

    result = pull(db_conn)

    assert (result["last_seen"], result["inserted"], result["complete"]) == (None, 3, True)
    assert get_watermark(db_conn) == "3"


def test_with_nothing_stored_and_nothing_listed_no_watermark_is_written(db_conn, worker_site):
    worker_site.serve_listing([[]])

    result = pull(db_conn)

    assert (result["inserted"], result["watermark"]) == (0, None)
    assert get_watermark(db_conn) is None


def test_with_nothing_new_the_watermark_records_where_the_stored_data_ends(db_conn, worker_site, entry_factory):
    store(db_conn, entry_factory, 200, 201)
    worker_site.serve_listing([[201, 200]])

    result = pull(db_conn)

    assert result["inserted"] == 0
    assert get_watermark(db_conn) == "201"                      # the row now exists


def test_an_unreadable_watermark_is_ignored_and_replaced(db_conn, worker_site, entry_factory, caplog):
    store(db_conn, entry_factory, 300)
    set_watermark(db_conn, "not-a-number")
    worker_site.serve_listing([[301, 300]])

    with caplog.at_level(logging.WARNING, logger="gradcafe.worker"):
        result = pull(db_conn)

    assert result["last_seen"] == 300                           # fell back to the newest stored p_id
    assert get_watermark(db_conn) == "301"
    assert "not a result id" in caplog.text


# --------------------------------------------------------------------------- #
#                         The rest of the same transaction                     #
# --------------------------------------------------------------------------- #

def test_every_pull_refreshes_the_snapshot_the_page_polls_for(db_conn, worker_site):
    worker_site.serve_listing([[102, 101]])
    pull(db_conn)
    first = snapshot_status(db_conn)

    pull(db_conn)                                               # nothing new this time
    second = snapshot_status(db_conn)

    assert first["total_entries"] == second["total_entries"] == 2
    assert second["computed_at"] > first["computed_at"]         # so the page knows the pull finished


def test_a_blocked_scrape_raises_and_writes_nothing(db_conn, worker_site):
    worker_site.serve(ROBOTS_URL, page("User-agent: *\nDisallow: /\n"))

    with pytest.raises(PermissionError):
        pull(db_conn)

    assert count_rows(db_conn) == 0
    assert get_watermark(db_conn) is None
    assert snapshot_status(db_conn) is None


def test_a_failed_insert_rolls_back_the_watermark_too(db_conn, worker_site):
    set_watermark(db_conn, "100")
    worker_site.serve_listing([[2**40, 101]])                   # 2**40 does not fit p_id INTEGER

    with pytest.raises(psycopg.Error):
        pull(db_conn)

    assert count_rows(db_conn) == 0
    assert get_watermark(db_conn) == "100"                      # unchanged


def test_the_page_cap_still_stores_what_was_fetched_and_logs_the_gap(db_conn, worker_site, caplog):
    set_watermark(db_conn, "1")
    worker_site.serve_listing([[1000 + n] for n in range(60, 0, -1)])   # 60 pages of new entries

    with caplog.at_level(logging.WARNING, logger="gradcafe.incremental"):
        result = pull(db_conn)

    assert (result["pages"], result["inserted"], result["complete"]) == (50, 50, False)
    assert get_watermark(db_conn) == "1060"
    assert len(worker_site.sleeps) == 49                        # one polite pause between pages
    assert "page limit (50) reached" in caplog.text


@pytest.mark.parametrize("since", [-1, 2**31, 1.5, "102", True, [1]])
def test_a_bad_since_is_refused_before_anything_is_fetched(db_conn, worker_site, since):
    with pytest.raises(TaskError, match="since"):
        pull(db_conn, {"since": since})

    assert worker_site.requested == []


def test_an_unknown_payload_field_is_refused(db_conn, worker_site):
    with pytest.raises(TaskError, match="unexpected payload"):
        pull(db_conn, {"sinse": 5})

    assert worker_site.requested == []


# --------------------------------------------------------------------------- #
#                 The pieces: SeenUpTo, NewEntries, the data folder            #
# --------------------------------------------------------------------------- #

def test_seen_up_to_knows_every_id_at_or_below_the_watermark():
    seen = SeenUpTo(101)

    assert 100 in seen and 101 in seen
    assert 102 not in seen
    assert "100" not in seen                                    # only result ids (ints) count
    assert 1 not in SeenUpTo(None)                              # no watermark: nothing is known


def test_new_entries_reports_its_newest_id_and_whether_it_finished():
    batch = NewEntries([{"result_id": 7}, {"result_id": 9}], pages=1, resume_url=None)

    assert (batch.max_id, batch.complete) == (9, True)
    assert NewEntries([], pages=1, resume_url="next").max_id is None
    assert NewEntries([], pages=1, resume_url="next").complete is False


def test_the_scraper_keeps_its_files_in_scrape_data_dir(db_conn, worker_site, tmp_path):
    worker_site.serve_listing([[5]])

    pull(db_conn)

    assert (tmp_path / "scrape" / "robots.txt").is_file()       # the copy of robots.txt it obeyed


def test_without_scrape_data_dir_the_scraper_uses_the_temp_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(incremental_scraper.tempfile, "gettempdir", lambda: str(tmp_path))

    assert incremental_scraper.scrape_data_dir() == tmp_path / "gradcafe_scrape"


def test_fetch_new_entries_with_no_pages_allowed_logs_the_gap(worker_site, caplog):
    worker_site.serve_listing([[5]])

    with caplog.at_level(logging.WARNING, logger="gradcafe.incremental"):
        batch = fetch_new_entries(4, max_pages=0)

    assert (batch.entries, batch.pages, batch.complete) == ([], 0, False)
    assert "entries older than result id None" in caplog.text


def test_newest_p_id_is_none_for_an_empty_table(db_conn, entry_factory):
    assert newest_p_id(db_conn) is None

    store(db_conn, entry_factory, 7, 42, 9)

    assert newest_p_id(db_conn) == 42
