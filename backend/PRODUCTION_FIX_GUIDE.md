# 🔧 Production Deployment Fix Guide

## Issues Identified

### 1. ❌ Database Tables Missing (PRIMARY ISSUE)
**Error:** `no such table: billing_document_headers`

The production PostgreSQL database is **empty**. You need to ingest the SAP data.

**Solution:**
```bash
# Run locally first to test
python ingest.py --data-dir ./sap-o2c-data --db-name dodgeai_o2c

# Or if using psycopg2 environment variables:
python ingest.py
```

This will:
- Create 19 normalized tables (billing_document_headers, products, business_partners, etc.)
- Load ~50K+ rows from JSONL files
- Create all necessary indexes

### 2. ⚠️ ChromaDB Cloud Config Mismatch (FIXED ✅)
**Issue:** .env had `CHROMA_TENANT` but code looked for `CHROMA_TENANT_ID`

**Fixed in:**
- ✅ search/semantic.py (already fixed)
- ✅ migrate_to_cloud.py (just fixed)
- ✅ verify_chromadb.py (just fixed)

### 3. ⚠️ Render Postgres Credentials (FOR PRODUCTION ONLY)
**Issue:** Local .env has Render DB_HOST which doesn't resolve locally

**For Render deployment, add these environment variables in Render dashboard:**
```
DB_HOST=dpg-d7235cruibrs73cr23e0-a
DB_PORT=5432
DB_USER=dodgeai_o2c_user
DB_PASSWORD=OQq3Ietbd4IOTs61uBuOHKpMctQY8YPJ
DB_NAME=dodgeai_o2c
CHROMA_API_KEY=ck-Atoibaf78hdHaZQBrbME3ge4VKLBWBhWJH1dQYCoEERo
CHROMA_TENANT=b5a4c0a6-9a4d-4560-a66e-baaab5fd8546
CHROMA_DATABASE=dodgeai-o2c
CHROMA_USE_CLOUD=true
REDIS_URL=redis://default:UUK4IYRGgQOZLvcaX7R13qOs80TzIf5W@redis-17737.c212.ap-south-1-1.ec2.cloud.redislabs.com:17737/0
```

---

## Action Plan

### ✅ Phase 1: Local Testing (DO THIS FIRST)

```bash
# 1. Verify ChromaDB Cloud works
python verify_chromadb.py

# Output should show:
# ✅ Found 'o2c_entities' with 510 documents!

# 2. Run diagnostics
python diagnostic.py

# 3. If database is empty, ingest data
python ingest.py
```

### ✅ Phase 2: Commit Fixes

```bash
git add search/semantic.py migrate_to_cloud.py verify_chromadb.py diagnostic.py
git commit -m "Fix: ChromaDB CHROMA_TENANT env var consistency + diagnostics"
git push
```

### ✅ Phase 3: Production Deployment (Render)

1. **If production DB is empty:**
   - Option A: Run migration from Render Shell: `python ingest.py`
   - Option B: Run locally and push data to production Postgres

2. **Verify Render environment variables are set:**
   - Go to Render dashboard → Your service → Environment
   - Add all 9 env variables listed above
   - Click "Save changes"
   - Wait for auto-redeploy

3. **Test production endpoint:**
   ```bash
   curl -X POST https://your-render-url/query/sync \
     -H "Content-Type: application/json" \
     -d '{"query": "Show me the top 5 customers by revenue"}'
   ```

---

## Troubleshooting

### If you still get "no such table" errors on Render:

```bash
# SSH into Render shell
# Run diagnostic
python diagnostic.py

# If DB is empty, ingest:
python ingest.py
```

### If ChromaDB Cloud still fails:

```bash
# Verify locally
python verify_chromadb.py

# If works locally but not on Render:
# → Check Render environment variables are actually set
# → Restart the service manually in dashboard
```

### If can't connect to Render Postgres locally:

That's **normal** - Render Postgres is only accessible from Render.  
Use local `.env` for development (already configured with Supabase).

---

## Current Status

| Component | Status | Action |
|-----------|--------|--------|
| ChromaDB Config | ✅ Fixed | Commit changes |
| Database Ingestion | ❌ TODO | Run `python ingest.py` |
| Render Env Vars | ⚠️ TODO | Add 9 vars to Render dashboard |
| Code Deployed | ✅ Ready | Push to git |

---

## Next Steps

1. **Immediately:** Run `python ingest.py` to load data
2. **Then:** Commit and push fixes
3. **Finally:** Update Render environment variables
