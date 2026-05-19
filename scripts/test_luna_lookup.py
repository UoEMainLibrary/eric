import argparse
import json

import bootstrap
from get_data import (
    LUNA_FETCH_URL,
    LUNA_HEADERS,
    REQUEST_TIMEOUT,
    build_luna_query_candidates,
    build_session,
    canonical_file_identifier,
    fetch_luna_identifier,
    luna_item_matches_filename,
    parse_luna_attributes,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Test how a source filename resolves against the LUNA API."
    )
    parser.add_argument(
        "filenames",
        nargs="+",
        help="One or more Archipelago/source filenames to test.",
    )
    parser.add_argument(
        "--show-results",
        action="store_true",
        help="Print a compact summary of every raw LUNA result returned per query.",
    )
    return parser.parse_args()


def fetch_query_results(session, field_name, candidate):
    params = {
        "fullData": "true",
        "bs": 10,
        "q": f"{field_name}={candidate}",
    }
    response = session.get(
        LUNA_FETCH_URL,
        params=params,
        headers=LUNA_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    results = response.json()
    if not isinstance(results, list):
        raise ValueError(f"Unexpected LUNA response type: {type(results)}")
    return results


def summarize_item(item):
    attrs = parse_luna_attributes(item)
    return {
        "id": item.get("id") or item.get("identity"),
        "repro_record_id": attrs.get("repro_record_id", ""),
        "repro_link_id": attrs.get("repro_link_id", ""),
        "mediafileName": attrs.get("mediafileName", ""),
        "matched": None,
    }


def main():
    args = parse_args()
    session = build_session()

    for index, source_filename in enumerate(args.filenames, start=1):
        if index > 1:
            print()

        canonical_file = canonical_file_identifier(source_filename)
        print(f"Source filename:   {source_filename}")
        print(f"Canonical file:    {canonical_file}")
        print("Query candidates:")
        for field_name, candidate in build_luna_query_candidates(source_filename):
            print(f"  - {field_name}={candidate}")

        try:
            luna_identifier, attempted_queries = fetch_luna_identifier(
                session,
                source_filename,
                cache={},
                verbose=False,
            )
        except Exception as exc:
            print(f"Lookup failed:     {exc}")
            continue

        print(f"Resolved LUNA ID:  {luna_identifier or 'NOT FOUND'}")
        if attempted_queries:
            print("Attempted queries:")
            for query in attempted_queries:
                print(f"  - {query}")

        print("Per-query results:")
        seen_ids = set()
        chosen_summary = None
        total_matches = 0

        for field_name, candidate in build_luna_query_candidates(source_filename):
            results = fetch_query_results(session, field_name, candidate)
            matching_results = [
                item for item in results if luna_item_matches_filename(item, source_filename)
            ]
            total_matches += len(matching_results)
            print(
                f"  - {field_name}={candidate}: {len(results)} result(s), "
                f"{len(matching_results)} matcher hit(s)"
            )

            if args.show_results:
                for item in results:
                    summary = summarize_item(item)
                    summary["matched"] = luna_item_matches_filename(item, source_filename)
                    print(f"    {json.dumps(summary, ensure_ascii=True, sort_keys=True)}")

            if chosen_summary is None:
                for item in matching_results:
                    item_id = item.get("id") or item.get("identity")
                    if item_id and item_id == luna_identifier:
                        chosen_summary = summarize_item(item)
                        chosen_summary["matched"] = True
                        break

            for item in results:
                item_id = item.get("id") or item.get("identity")
                if item_id:
                    seen_ids.add(item_id)

        print(f"Unique result IDs: {len(seen_ids)}")
        print(f"Matcher hits:      {total_matches}")
        if chosen_summary:
            print("Chosen item:")
            print(json.dumps(chosen_summary, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
