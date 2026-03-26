#!/usr/bin/env python3
"""
diagnostic.py — Comprehensive system health check
"""

import os
import sys
from dotenv import load_dotenv
import psycopg2

load_dotenv()

print("\n" + "="*80)
print("🔍 DodgeAI PRODUCTION DIAGNOSTIC CHECK")
print("="*80)

# ── 1. Environment Variables ──────────────────────────────────────────────────
print("\n📋 ENVIRONMENT VARIABLES:")
print("-" * 80)

db_host = os.getenv("DB_HOST", "").strip()
db_port = os.getenv("DB_PORT", "5432").strip()
db_user = os.getenv("DB_USER", "").strip()
db_password = os.getenv("DB_PASSWORD", "").strip()
db_name = os.getenv("DB_NAME", "").strip()
database_url = os.getenv("DATABASE_URL", "").strip()

print(f"DB_HOST:       {db_host if db_host else '❌ NOT SET'}")
print(f"DB_PORT:       {db_port if db_port else '❌ NOT SET'}")
print(f"DB_USER:       {db_user if db_user else '❌ NOT SET'}")
print(f"DB_PASSWORD:   {'*' * len(db_password) if db_password else '❌ NOT SET'} ({len(db_password)} chars)")
print(f"DB_NAME:       {db_name if db_name else '❌ NOT SET'}")
print(f"DATABASE_URL:  {database_url[:40]}..." if database_url else "❌ NOT SET")

# ── 2. ChromaDB Configuration ─────────────────────────────────────────────────
print("\n🗂️  CHROMADB CONFIGURATION:")
print("-" * 80)

chroma_use_cloud = os.getenv("CHROMA_USE_CLOUD", "false").lower() == "true"
chroma_api_key = os.getenv("CHROMA_API_KEY", "").strip()
chroma_tenant = os.getenv("CHROMA_TENANT_ID", "").strip()
chroma_db = os.getenv("CHROMA_DATABASE", "").strip()

print(f"CHROMA_USE_CLOUD:  {chroma_use_cloud}")
print(f"CHROMA_API_KEY:    {chroma_api_key[:20]}..." if chroma_api_key else "❌ NOT SET")
print(f"CHROMA_TENANT_ID:  {chroma_tenant[:20]}..." if chroma_tenant else "❌ NOT SET")
print(f"CHROMA_DATABASE:   {chroma_db if chroma_db else '❌ NOT SET'}")

_ROW_LIMIT = 200
_TIMEOUT = 10.0  # seconds

# Test PostgreSQL Connection ─────────────────────────────────────────────────
print("\n🔌 POSTGRESQL CONNECTION TEST:")
print("-" * 80)

tables = []

try:
    conn = psycopg2.connect(
        host=db_host,
        port=int(db_port),
        user=db_user,
        password=db_password,
        database=db_name,
        connect_timeout=5
    )
    print(f"✅ Connected to {db_host}:{db_port}/{db_name}")
    
    # Get list of tables
    cursor = conn.cursor()
    cursor.execute("""
        SELECT table_name FROM information_schema.tables 
        WHERE table_schema='public' 
        ORDER BY table_name;
    """)
    tables = [row[0] for row in cursor.fetchall()]
    
    if tables:
        print(f"✅ Found {len(tables)} tables:")
        for t in tables[:10]:  # Show first 10
            cursor.execute(f"SELECT COUNT(*) FROM {t}")
            count = cursor.fetchone()[0]
            print(f"   • {t}: {count} rows")
        if len(tables) > 10:
            print(f"   ... and {len(tables) - 10} more tables")
    else:
        print("❌ NO TABLES FOUND — Database is empty!")
        print("   → You need to run: python ingest.py")
    
    cursor.close()
    conn.close()
    
except psycopg2.OperationalError as e:
    print(f"❌ Connection failed: {e}")
    print(f"   Check: DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME in .env")
    print(f"\n   💡 For production:")
    print(f"      Render will provide these via Environment Variables in dashboard")
    print(f"      Add them instead of committing to .env")
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()

# ── 4. Redis Configuration ────────────────────────────────────────────────────
print("\n💾 REDIS CONFIGURATION:")
print("-" * 80)

redis_url = os.getenv("REDIS_URL", "").strip()
print(f"REDIS_URL:     {redis_url[:40]}..." if redis_url else "❌ NOT SET")

# Try to connect to Redis
try:
    import redis
    r = redis.from_url(redis_url, decode_responses=True)
    r.ping()
    print(f"✅ Redis connected successfully")
    info = r.info('memory')
    print(f"   Memory used: {info['used_memory_human']}")
except ImportError:
    print("⚠️  Redis library not installed")
except Exception as e:
    print(f"⚠️  Redis connection failed: {e}")

# ── 5. Summary & Recommendations ──────────────────────────────────────────────
print("\n" + "="*80)
print("📊 SUMMARY & RECOMMENDATIONS:")
print("="*80)

issues = []

if not db_host:
    issues.append("❌ DB_HOST not set")
if not db_name:
    issues.append("❌ DB_NAME not set")
if tables and len(tables) == 0:
    issues.append("❌ Database has no tables — run: python ingest.py")
elif 'billing_document_headers' not in tables:
    issues.append("❌ billing_document_headers table missing — run: python ingest.py")

if chroma_use_cloud and not (chroma_api_key and chroma_tenant):
    issues.append("⚠️  CHROMA_USE_CLOUD=true but credentials missing")

if issues:
    print("\n⚠️  ISSUES FOUND:")
    for issue in issues:
        print(f"  {issue}")
else:
    print("\n✅ All checks passed!")

print("\n" + "="*80)
