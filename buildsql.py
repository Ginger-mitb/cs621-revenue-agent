import sqlite3, pathlib
import pandas as pd

def build_sqlite(snapshot_dir="data/snapshot", db_path="data/thelook.sqlite"):
    db = sqlite3.connect(db_path)
    for p in sorted(pathlib.Path(snapshot_dir).glob("*.parquet")):
        df = pd.read_parquet(p)
        for c in df.select_dtypes(include=["datetimetz", "datetime"]).columns:
            df[c] = df[c].dt.strftime("%Y-%m-%d %H:%M:%S")     # SQLite 没有时间戳类型
        df.to_sql(p.stem, db, if_exists="replace", index=False)
        print(f"{p.stem:<20} {len(df):>8,} rows")
    db.commit(); db.close()

build_sqlite()
con = sqlite3.connect("file:data/thelook.sqlite?mode=ro", uri=True)   # agent 的查询工具用这个
