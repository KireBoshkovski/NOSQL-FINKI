#!/usr/bin/env python3
"""
Benchmark harness for the TMDB project (MongoDB side).

Runs each of the 8 queries REPEATS times, records executionTimeMillis from
MongoDB's own explain("executionStats"), averages them, prints a table, and
saves a bar chart.

It measures BOTH aggregation models for the genre query (flat vs embedded),
which is the comparison your second model exists to demonstrate.

If you later get the PostgreSQL timings, drop them into POSTGRES_MS below and
re-run with --compare to get a side-by-side MongoDB-vs-PostgreSQL chart.

Run:
  python3 benchmark_mongo.py                 # MongoDB only, 50 runs each
  python3 benchmark_mongo.py --repeats 100
  python3 benchmark_mongo.py --compare       # also plot PostgreSQL bars
"""
import argparse
import statistics
import matplotlib
matplotlib.use("Agg")          # no display needed, save to file
import matplotlib.pyplot as plt
from pymongo import MongoClient


# ----------------------------------------------------------------------
# Optional: paste your teammates' PostgreSQL timings here (avg ms per query)
# Leave as None until you have them; --compare uses these.
# ----------------------------------------------------------------------
POSTGRES_MS = {
    "Q1 year+rating filter": None,
    "Q2 language count": None,
    "Q3 genre membership": None,
    "Q4 genre (1 doc/JOIN)": None,
    "Q5 genres ranked": None,
    "Q6 avg rating/year": None,
    "Q7 avg rating/genre": None,
    "Q8 lang popularity": None,
}


def time_find(coll, filt, repeats, sort=None, limit=None):
    """Average executionTimeMillis of a find() over `repeats` runs."""
    times = []
    for _ in range(repeats):
        cur = coll.find(filt)
        if sort:
            cur = cur.sort(*sort)
        if limit:
            cur = cur.limit(limit)
        stats = cur.explain()["executionStats"]
        times.append(stats["executionTimeMillis"])
    return statistics.mean(times)


def time_count(coll, filt, repeats):
    """Average time of a count, via the equivalent find().explain()."""
    return time_find(coll, filt, repeats)


def time_agg(db, collname, pipeline, repeats):
    """Average executionTimeMillis of an aggregate() over `repeats` runs.

    Aggregate explain only returns timings when verbosity is
    'executionStats' — the default returns just the plan (all zeros).
    """
    times = []
    for _ in range(repeats):
        out = db.command(
            {"explain": {"aggregate": collname, "pipeline": pipeline,
                         "cursor": {}},
             "verbosity": "executionStats"})
        times.append(_extract_agg_ms(out))
    return statistics.mean(times)


def _extract_agg_ms(explain_out):
    """Pull the whole-pipeline executionTimeMillis from an executionStats
    aggregate explain. The cursor stage carries the authoritative total;
    fall back to the max per-stage estimate if the layout differs."""
    stages = explain_out.get("stages")
    if stages:
        # stage 0 holds the $cursor with real executionStats
        cur = stages[0].get("$cursor", {})
        es = cur.get("executionStats", {})
        if "executionTimeMillis" in es:
            # add the downstream stage estimates so group/unwind cost counts
            stage_est = max(
                (s.get("executionTimeMillisEstimate", 0) for s in stages),
                default=0)
            return max(es["executionTimeMillis"], stage_est)
    # single-collection explain (no pipeline stages)
    if "executionStats" in explain_out:
        return explain_out["executionStats"].get("executionTimeMillis", 0)
    return 0


def run(db, repeats):
    results = {}

    # ---- TIER 1: simple filters ----
    results["Q1 year+rating filter"] = time_find(
        db.movies, {"year": 2020, "vote_average": {"$gt": 7}},
        repeats, sort=("popularity", -1), limit=5)

    results["Q2 language count"] = time_count(
        db.movies, {"original_language": "fr"}, repeats)

    results["Q3 genre membership"] = time_count(
        db.movies, {"genre_ids": 27}, repeats)

    # ---- TIER 2: combining / the two-model comparison ----
    # Q4a: genre via FLAT model + $unwind (scan + normalise)
    flat_genre = time_agg(db, "movies", [
        {"$match": {"genre_ids": 27}},
        {"$project": {"title": 1, "vote_average": 1}},
        {"$sort": {"vote_average": -1}},
        {"$limit": 5},
    ], repeats)
    # Q4b: same answer via EMBEDDED model (single doc read)
    embedded_genre = time_find(
        db.movies_by_genre, {"genre_name": "Horror"}, repeats)

    results["Q4 genre (flat+unwind)"] = flat_genre
    results["Q4 genre (embedded)"] = embedded_genre

    results["Q5 genres ranked"] = time_agg(db, "movies_by_genre", [
        {"$project": {"genre_name": 1, "movie_count": 1}},
        {"$sort": {"movie_count": -1}},
        {"$limit": 5},
    ], repeats)

    # ---- TIER 3: aggregated reports ----
    results["Q6 avg rating/year"] = time_agg(db, "movies", [
        {"$match": {"vote_count": {"$gt": 0}}},
        {"$group": {"_id": "$year",
                    "avg_rating": {"$avg": "$vote_average"},
                    "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ], repeats)

    results["Q7 avg rating/genre"] = time_agg(db, "movies", [
        {"$match": {"vote_count": {"$gt": 0}}},
        {"$unwind": "$genres"},
        {"$group": {"_id": "$genres.name",
                    "avg_rating": {"$avg": "$vote_average"},
                    "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": 3}}},
        {"$sort": {"avg_rating": -1}},
        {"$limit": 8},
    ], repeats)

    results["Q8 lang popularity"] = time_agg(db, "movies", [
        {"$group": {"_id": "$original_language",
                    "n": {"$sum": 1},
                    "avg_pop": {"$avg": "$popularity"}}},
        {"$match": {"n": {"$gte": 10}}},
        {"$sort": {"n": -1}},
        {"$limit": 8},
    ], repeats)

    return results


def print_table(results):
    print(f"\n{'Query':<28}{'avg ms (MongoDB)':>18}")
    print("-" * 46)
    for k, v in results.items():
        print(f"{k:<28}{v:>18.3f}")


def make_chart(results, compare, outpath):
    labels = list(results.keys())
    mongo_vals = list(results.values())

    fig, ax = plt.subplots(figsize=(11, 6))

    if compare:
        # align postgres values to the same query labels where available
        import numpy as np
        x = np.arange(len(labels))
        width = 0.38
        pg_vals = [POSTGRES_MS.get(l) or 0 for l in labels]
        ax.bar(x - width/2, mongo_vals, width, label="MongoDB", color="#3FA34D")
        ax.bar(x + width/2, pg_vals, width, label="PostgreSQL", color="#2E5EAA")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=40, ha="right")
        ax.legend()
        title = "Query execution time: MongoDB vs PostgreSQL"
    else:
        x = list(range(len(labels)))
        ax.bar(x, mongo_vals, color="#3FA34D")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=40, ha="right")
        title = "MongoDB query execution time (avg of repeated runs)"

    ax.set_ylabel("executionTimeMillis (avg)")
    ax.set_title(title)
    plt.tight_layout()
    plt.savefig(outpath, dpi=130)
    print(f"\nChart saved to {outpath}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="mongodb://localhost:27017")
    ap.add_argument("--db", default="tmdb")
    ap.add_argument("--repeats", type=int, default=50)
    ap.add_argument("--compare", action="store_true",
                    help="also plot PostgreSQL bars from POSTGRES_MS")
    ap.add_argument("--out", default="benchmark_chart.png")
    args = ap.parse_args()

    db = MongoClient(args.uri)[args.db]
    print(f"Benchmarking {args.db} — {args.repeats} runs per query...")
    results = run(db, args.repeats)
    print_table(results)
    make_chart(results, args.compare, args.out)
    print("\nDone. Use these averaged numbers in section 5 of your report.")


if __name__ == "__main__":
    main()
