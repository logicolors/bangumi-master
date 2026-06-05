"""Fetch Bangumi anime data and export the browser game's JSON files."""

import json
import os
import sqlite3
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("BANGUMI_DB_PATH", ROOT / ".cache" / "anime.db"))
LIST_PATH = ROOT / "data" / "anime_list.json"
META_PATH = ROOT / "data" / "anime_meta.json"

API_BASE = "https://api.bgm.tv"
USER_AGENT = "BangumiMasterDataUpdater/1.0 (https://github.com/logicolors/bangumi-master)"
PAGE_SIZE = 20
RATE_DELAY = 0.6


def gen_windows():
    windows = []

    def add(y1, m1, d1, y2, m2, d2):
        windows.append((f"{y1:04d}-{m1:02d}-{d1:02d}", f"{y2:04d}-{m2:02d}-{d2:02d}"))

    add(1900, 1, 1, 1960, 1, 1)

    for year in range(1960, 1990, 5):
        add(year, 1, 1, year + 5, 1, 1)

    for year in range(1990, 2016):
        add(year, 1, 1, year + 1, 1, 1)

    for year in range(2016, date.today().year + 2):
        for start_month, end_month in [(1, 4), (4, 7), (7, 10), (10, 1)]:
            end_year = year + 1 if end_month == 1 else year
            add(year, start_month, 1, end_year, end_month, 1)

    return windows


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS anime (
            id          INTEGER PRIMARY KEY,
            name        TEXT,
            name_cn     TEXT,
            score       REAL,
            vote_count  INTEGER,
            rank        INTEGER,
            air_date    TEXT,
            image_url   TEXT,
            tags        TEXT,
            nsfw        INTEGER
        );

        CREATE TABLE IF NOT EXISTS fetch_state (
            window_key  TEXT PRIMARY KEY,
            done        INTEGER DEFAULT 0
        );
    """)
    conn.commit()


def search(start_date, end_date, offset):
    url = f"{API_BASE}/v0/search/subjects?limit={PAGE_SIZE}&offset={offset}"
    body = json.dumps({
        "keyword": "",
        "sort": "rank",
        "filter": {
            "type": [2],
            "air_date": [f">={start_date}", f"<{end_date}"],
            "nsfw": False,
        },
    }).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def window_done(conn, key):
    row = conn.execute("SELECT done FROM fetch_state WHERE window_key = ?", (key,)).fetchone()
    return row is not None and row[0] == 1


def mark_window_done(conn, key):
    conn.execute("INSERT OR REPLACE INTO fetch_state(window_key, done) VALUES(?, 1)", (key,))
    conn.commit()


def upsert_anime(conn, items):
    rows = []
    for item in items:
        rating = item.get("rating") or {}
        rank = rating.get("rank") or None
        if rank == 0:
            rank = None
        rows.append((
            item["id"],
            item.get("name"),
            item.get("name_cn") or item.get("name"),
            rating.get("score"),
            rating.get("total", 0),
            rank,
            item.get("date"),
            (item.get("images") or {}).get("common"),
            json.dumps([tag["name"] for tag in item.get("tags", [])], ensure_ascii=False),
            1 if item.get("nsfw") else 0,
        ))
    conn.executemany("""
        INSERT OR REPLACE INTO anime
            (id, name, name_cn, score, vote_count, rank, air_date, image_url, tags, nsfw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()


def fetch_window(conn, start, end):
    key = f"{start}_{end}"
    if window_done(conn, key):
        return 0

    offset = 0
    fetched = 0
    total = None

    while True:
        for attempt in range(3):
            try:
                data = search(start, end, offset)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)

        items = data.get("data") or []
        if total is None:
            total = data.get("total", 0)
        if not items:
            break

        upsert_anime(conn, items)
        fetched += len(items)
        offset += len(items)

        if offset >= min(total, 1000):
            break

        time.sleep(RATE_DELAY)

    mark_window_done(conn, key)
    return fetched


def export_json(conn):
    rows = conn.execute("""
        SELECT id, name, name_cn, score, rank, image_url, vote_count, tags, air_date
        FROM anime
        WHERE vote_count >= 100
          AND score IS NOT NULL
          AND COALESCE(nsfw, 0) = 0
        ORDER BY
            CASE WHEN rank IS NULL THEN 1 ELSE 0 END,
            rank ASC,
            score DESC
    """).fetchall()

    anime_list = [
        {
            "id": row[0],
            "name": row[1],
            "name_cn": row[2] or row[1],
            "score": row[3],
            "rank": row[4],
            "image_url": row[5],
            "vote_count": row[6],
            "tags": json.loads(row[7]) if row[7] else [],
            "air_date": row[8],
        }
        for row in rows
    ]

    LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIST_PATH.write_text(
        json.dumps(anime_list, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    META_PATH.write_text(
        json.dumps({"collected_at": date.today().isoformat()}, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(anime_list)


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)
        windows = gen_windows()
        total_new = 0
        for index, (start, end) in enumerate(windows, 1):
            fetched = fetch_window(conn, start, end)
            total_new += fetched
            print(f"[{index:>3}/{len(windows)}] {start} -> {end} +{fetched}")
            time.sleep(RATE_DELAY)

        exported = export_json(conn)

    print(f"Done. New fetched rows: {total_new}. Exported anime: {exported}.")


if __name__ == "__main__":
    main()
