import sqlite3
import httpx
import os
import sys

TURSO_DB_URL = os.getenv("TURSO_DB_URL", "https://main-hailxd.aws-ap-northeast-1.turso.io")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN_WRITE", "")
IMG_BASE = "https://i.pximg.net/img-original/img/"
BATCH_SIZE = 200

def turso_url():
    base = TURSO_DB_URL.rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    return base + "/v2/pipeline"

def turso_post(stmts):
    headers = {"Authorization": f"Bearer {TURSO_AUTH_TOKEN}", "Content-Type": "application/json"}
    body = {"requests": [{"type": "execute", "stmt": s} for s in stmts] + [{"type": "close"}]}
    r = httpx.post(turso_url(), json=body, headers=headers, timeout=120)
    r.raise_for_status()
    return r.json()

def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\jyeal\Documents\programming-related\Pixif\Client\db.sqlite"
    conn = sqlite3.connect(db_path)

    turso_post([
        {"sql": "CREATE TABLE IF NOT EXISTS searches (id TEXT PRIMARY KEY, query TEXT, post_ids TEXT, created_at INTEGER)"},
        {"sql": "CREATE TABLE IF NOT EXISTS scans (post_id TEXT PRIMARY KEY, url TEXT, exif_type INTEGER)"},
    ])

    rows = conn.execute("SELECT post_id, url, exif_type FROM pixif_cache").fetchall()
    print(f"Found {len(rows)} rows in local db")

    stmts = []
    for post_id, url, exif_type in rows:
        short_url = url.replace(IMG_BASE, "", 1) if url else ""
        args = [
            {"type": "text", "value": str(post_id)},
            {"type": "text", "value": short_url},
        ]
        if exif_type is not None:
            args.append({"type": "integer", "value": str(exif_type)})
        else:
            args.append({"type": "null"})
        stmts.append({
            "sql": "INSERT OR IGNORE INTO scans (post_id, url, exif_type) VALUES (?, ?, ?)",
            "args": args,
        })

    uploaded = 0
    for i in range(0, len(stmts), BATCH_SIZE):
        batch = stmts[i:i+BATCH_SIZE]
        turso_post(batch)
        uploaded += len(batch)
        print(f"  {uploaded}/{len(stmts)}")

    print(f"Done. Uploaded {len(stmts)} rows to Turso.")
    conn.close()

if __name__ == "__main__":
    main()
