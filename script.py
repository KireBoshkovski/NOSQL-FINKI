#!/usr/bin/env python3
"""
Import TMDB JSONL files into MongoDB in two aggregation models:
  1. FLAT     -> collection `movies`        : one document per movie
  2. BY-GENRE -> collection `movies_by_genre`: one document per genre, movies embedded

Usage:
  python3 mongo_import.py /path/to/folder-with-json-files
  python3 mongo_import.py /path/to/folder --uri mongodb://localhost:27017 --db tmdb
"""
import sys, os, glob, json, argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from genres import GENRE_MAP


def parse_year(release_date, fallback_year):
    """Year from release_date if present, else the filename year."""
    if release_date:                      # handles "" and None
        try:
            return datetime.strptime(release_date[:10], "%Y-%m-%d").year
        except (ValueError, TypeError):
            pass
    return fallback_year


def clean_movie(raw, fallback_year):
    """Normalise one raw record into a flat movie document."""
    codes = raw.get("genre_ids") or []
    genres = [{"id": c, "name": GENRE_MAP.get(c, "Unknown")} for c in codes]
    rd = raw.get("release_date") or None  # turn "" into None
    return {
        "_id": raw["id"],                 # use TMDB id as the primary key
        "imdb_id": raw.get("id_imdb"),
        "title": raw.get("title"),
        "original_title": raw.get("original_title"),
        "original_language": raw.get("original_language"),
        "release_date": rd,
        "year": parse_year(rd, fallback_year),
        "vote_average": raw.get("vote_average", 0) or 0,
        "vote_count": raw.get("vote_count", 0) or 0,
        "popularity": raw.get("popularity", 0) or 0,
        "adult": bool(raw.get("adult", False)),
        "video": bool(raw.get("video", False)),
        "overview": raw.get("overview") or "",
        "poster_path": raw.get("poster_path"),
        "genre_ids": codes,               # raw codes kept
        "genres": genres,                 # codes + names kept (both, as requested)
    }


def iter_movies(folder):
    """Yield cleaned movie docs from every *.json (JSONL) file in folder."""
    files = sorted(glob.glob(os.path.join(folder, "*.json")))
    if not files:
        raise SystemExit(f"No .json files found in {folder}")
    for path in files:
        # filename like 2020.json -> fallback year 2020
        base = os.path.splitext(os.path.basename(path))[0]
        fallback_year = int(base) if base.isdigit() else None
        with open(path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"  ! skip {os.path.basename(path)}:{line_no} ({e})")
                    continue
                if "id" not in raw:
                    continue
                yield clean_movie(raw, fallback_year)


def build_by_genre(movies):
    """Group flat movies into one document per genre (movies embedded)."""
    buckets = {}
    for m in movies:
        # a light embedded copy — avoid duplicating the huge overview text
        light = {
            "_id": m["_id"], "title": m["title"], "year": m["year"],
            "vote_average": m["vote_average"], "vote_count": m["vote_count"],
            "popularity": m["popularity"], "original_language": m["original_language"],
        }
        if not m["genres"]:
            b = buckets.setdefault(-1, {"_id": -1, "genre_id": -1,
                                        "genre_name": "(none)", "movies": []})
            b["movies"].append(light)
        for g in m["genres"]:
            b = buckets.setdefault(g["id"], {"_id": g["id"], "genre_id": g["id"],
                                             "genre_name": g["name"], "movies": []})
            b["movies"].append(light)
    # add a movie_count for convenience
    for b in buckets.values():
        b["movie_count"] = len(b["movies"])
    return list(buckets.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="folder containing the per-year JSONL files")
    ap.add_argument("--uri", default="mongodb://localhost:27017")
    ap.add_argument("--db", default="tmdb")
    args = ap.parse_args()

    from pymongo import MongoClient, ASCENDING

    client = MongoClient(args.uri)
    db = client[args.db]

    # ---- Model 1: FLAT ----
    flat = list(iter_movies(args.folder))
    print(f"Parsed {len(flat)} movies")
    db.movies.drop()
    if flat:
        db.movies.insert_many(flat, ordered=False)
    db.movies.create_index([("year", ASCENDING)])
    db.movies.create_index([("genre_ids", ASCENDING)])
    db.movies.create_index([("vote_average", ASCENDING)])
    db.movies.create_index([("original_language", ASCENDING)])
    print(f"  movies: {db.movies.count_documents({})} docs inserted")

    # ---- Model 2: BY-GENRE ----
    by_genre = build_by_genre(flat)
    db.movies_by_genre.drop()
    if by_genre:
        db.movies_by_genre.insert_many(by_genre, ordered=False)
    print(f"  movies_by_genre: {db.movies_by_genre.count_documents({})} genre docs")
    for b in sorted(by_genre, key=lambda x: -x["movie_count"])[:5]:
        print(f"    {b['genre_name']:<16} {b['movie_count']} movies")

    print("Done.")


if __name__ == "__main__":
    main()