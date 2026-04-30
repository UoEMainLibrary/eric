import csv

from app import app, db, LunaRoute


CSV_FILE = "recent_item_tinyurls.csv"


with app.app_context():
    created = 0
    updated = 0
    skipped = 0

    with open(CSV_FILE, newline="", encoding="utf-8") as handle:
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
