import csv

import bootstrap
from app import app, db, LunaRoute
from project_paths import RECENT_TINYURLS_CSV


def main():
    with app.app_context():
        created = 0
        updated = 0
        skipped = 0

        with open(RECENT_TINYURLS_CSV, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)

            for row in reader:
                token = (row.get("token") or "").strip()
                route_type = (row.get("route_type") or "").strip()
                target_url = (row.get("target_url") or "").strip()

                if not token or not route_type or not target_url:
                    skipped += 1
                    continue

                existing = LunaRoute.query.filter_by(token=token).first()
                if existing:
                    changed = False

                    if existing.route_type != route_type:
                        existing.route_type = route_type
                        changed = True

                    if existing.target_url != target_url:
                        existing.target_url = target_url
                        changed = True

                    if changed:
                        updated += 1
                    else:
                        skipped += 1
                    continue

                db.session.add(
                    LunaRoute(
                        token=token,
                        route_type=route_type,
                        target_url=target_url,
                    )
                )
                created += 1

            db.session.commit()

        print(
            f"Luna route import complete! "
            f"Created: {created}, Updated: {updated}, Skipped: {skipped}"
        )


if __name__ == "__main__":
    main()
