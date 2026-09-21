
import os
import sys
import requests
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

# ------------------------------------------------------------------
# Step 1: Load environment variables
# ------------------------------------------------------------------
load_dotenv()

DEBUG = os.getenv("DEBUG", "False") == "True"

DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")

API_ENDPOINT = os.getenv("CLOUDSIGMA_API_ENDPOINT", "").rstrip("/") + "/"
CS_USERNAME = os.getenv("CLOUDSIGMA_USERNAME")
CS_PASSWORD = os.getenv("CLOUDSIGMA_PASSWORD")

print("====================================")
print("CloudSigma -> PostgreSQL Sync")
print("====================================")
print(f"DEBUG mode: {DEBUG}")
print(f"CloudSigma Endpoint: {API_ENDPOINT}")
print(f"CloudSigma Username: {CS_USERNAME}")
print(f"Postgres DB: {DB_NAME} @ {DB_HOST}:{DB_PORT}")
print()

if "YOUR-CLOUDSIGMA-API" in API_ENDPOINT:
    print("ERROR: CLOUDSIGMA_API_ENDPOINT is still a placeholder.")
    print("Set it to your actual region, e.g. https://zrh.cloudsigma.com/api/2.0/")
    sys.exit(1)

if not CS_USERNAME or not CS_PASSWORD:
    print("ERROR: CloudSigma username/password not set in .env")
    sys.exit(1)

if not DB_NAME or not DB_USER or not DB_PASSWORD:
    print("ERROR: Database credentials not set in .env")
    sys.exit(1)

# ------------------------------------------------------------------
# Step 2: Set up HTTP Basic Auth (no separate login step needed)
# ------------------------------------------------------------------
session = requests.Session()
session.headers.update({"Accept": "application/json"})
session.auth = HTTPBasicAuth(CS_USERNAME, CS_PASSWORD)

# Quick check: verify credentials work by hitting a lightweight endpoint
check_url = f"{API_ENDPOINT}profile/"
print(f"Verifying credentials at: {check_url}")
check_response = session.get(check_url)
print(f"Auth Check Status: {check_response.status_code}")

if check_response.status_code != 200:
    print("AUTHENTICATION FAILED")
    print(check_response.text)
    sys.exit(1)

print("AUTHENTICATION SUCCESS")
print()

# ------------------------------------------------------------------
# Step 3: Fetch drives from CloudSigma
# ------------------------------------------------------------------
drives_url = f"{API_ENDPOINT}drives/"
drives_response = session.get(drives_url)

print(f"Drives Status: {drives_response.status_code}")

if drives_response.status_code != 200:
    print("FAILED TO FETCH DRIVES")
    print(drives_response.text)
    sys.exit(1)

drives = drives_response.json().get("objects", [])
print(f"Found {len(drives)} drive(s) on CloudSigma.")
print()

# ------------------------------------------------------------------
# Step 4: Connect to PostgreSQL
# ------------------------------------------------------------------
try:
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )
    print("Connected to PostgreSQL successfully.")
except Exception as e:
    print("FAILED TO CONNECT TO POSTGRESQL")
    print(e)
    sys.exit(1)

cur = conn.cursor()

# ------------------------------------------------------------------
# Step 5: Create table if it doesn't exist
# ------------------------------------------------------------------
cur.execute("""
    CREATE TABLE IF NOT EXISTS cloudsigma_drives (
        uuid TEXT PRIMARY KEY,
        name TEXT,
        size BIGINT,
        status TEXT,
        media TEXT,
        synced_at TIMESTAMP DEFAULT NOW()
    );
""")
conn.commit()

# ------------------------------------------------------------------
# Step 6: Insert / update drive records
# ------------------------------------------------------------------
if drives:
    rows = [
        (
            d.get("uuid"),
            d.get("name") or "(unnamed)",
            d.get("size"),
            d.get("status"),
            d.get("media"),
        )
        for d in drives
    ]

    execute_values(
        cur,
        """
        INSERT INTO cloudsigma_drives (uuid, name, size, status, media)
        VALUES %s
        ON CONFLICT (uuid) DO UPDATE SET
            name = EXCLUDED.name,
            size = EXCLUDED.size,
            status = EXCLUDED.status,
            media = EXCLUDED.media,
            synced_at = NOW();
        """,
        rows,
    )
    conn.commit()
    print(f"Synced {len(rows)} drive(s) into PostgreSQL table 'cloudsigma_drives'.")
else:
    print("No drives found to sync.")

# ------------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------------
cur.close()
conn.close()
print()
print("Done.")