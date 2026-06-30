from sqlalchemy import create_engine, text
import pandas as pd

e = create_engine("postgresql+psycopg2://postgres:mypassword@localhost:5432/MA_TAMA")

YES = "כן"   # כן

with e.connect() as c:
    rows = c.execute(text("""
        SELECT
            building_stage,
            sw_tama_38_chadash,
            (open_request     IS NOT NULL) AS has_form1,
            (permission_date  IS NOT NULL) AS has_permit,
            (tr_hathalat_bniya IS NOT NULL) AS has_build,
            (finished         IS NOT NULL) AS has_form4
        FROM "TLV".permits
        WHERE sw_tama_38 = :yes OR sw_tama_38_chadash = :yes OR sw_tama_38_tosefet = :yes
    """), {"yes": YES}).fetchall()
    df = pd.DataFrame(rows, columns=["building_stage","sw_tama_38_chadash","has_form1","has_permit","has_build","has_form4"])

print(f"Total TAMA38 permits : {len(df):,}")
print(f"Has Form1 date       : {df.has_form1.sum():,}")
print(f"Has permit date      : {df.has_permit.sum():,}")
print(f"Has construction date: {df.has_build.sum():,}")
print(f"Has Form4 (finished) : {df.has_form4.sum():,}")
print()
print("Building stage breakdown:")
print(df.building_stage.value_counts().to_string())
print()
print("Track 2 (chadash):", (df.sw_tama_38_chadash == YES).sum())
