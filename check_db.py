from sqlalchemy import create_engine, text
e = create_engine("postgresql+psycopg2://postgres:mypassword@localhost:5432/MA_TAMA")
tables = ["permits", "addresses", "buildings", "archive_timelines", "neighborhoods"]
with e.connect() as c:
    for t in tables:
        q = 'SELECT COUNT(*) FROM "TLV".' + t
        try:
            n = c.execute(text(q)).scalar()
            print(f"  {t:<22} {n:>8,} rows")
        except Exception as ex:
            print(f"  {t:<22} MISSING ({type(ex).__name__})")
