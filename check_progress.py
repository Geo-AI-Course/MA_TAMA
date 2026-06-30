from sqlalchemy import create_engine, text
e = create_engine("postgresql+psycopg2://postgres:mypassword@localhost:5432/MA_TAMA")
with e.connect() as c:
    try:
        n    = c.execute(text('SELECT COUNT(*) FROM "TLV".archive_timelines')).scalar()
        f4   = c.execute(text('SELECT COUNT(*) FROM "TLV".archive_timelines WHERE form4 IS NOT NULL')).scalar()
        any_ = c.execute(text('SELECT COUNT(*) FROM "TLV".archive_timelines WHERE form1 IS NOT NULL OR permit_verbal IS NOT NULL')).scalar()
        last = c.execute(text('SELECT k_rechov, ms_bayit, scraped_at FROM "TLV".archive_timelines ORDER BY scraped_at DESC LIMIT 3')).fetchall()
        total = 2945
        pct = round(n / total * 100, 1)
        eta_min = round((total - n) * 12 / 60, 0)
        print(f"Scraped      : {n:,} / {total:,}  ({pct}%)")
        print(f"With Form4   : {f4:,}")
        print(f"With any date: {any_:,}")
        print(f"ETA          : ~{int(eta_min)} min remaining")
        print(f"\nLast 3 scraped:")
        for r in last:
            print(f"  k={r[0]}  m={r[1]}  at {r[2]}")
    except Exception as ex:
        print("archive_timelines not found yet —", ex)
