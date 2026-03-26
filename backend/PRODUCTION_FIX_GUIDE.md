# 🔧 Production Deployment Fix Guide

## Issues Identified

### 1. ❌ Database Tables Missing (PRIMARY ISSUE)
**Error:** `no such table: billing_document_headers`

The Supabase PostgreSQL database is **empty**. You need to ingest the SAP data.

**Solution:**
```bash
# Run locally to load data into Supabase
python ingest.py --data-dir ./sap-o2c-data --db-name postgres

# Or simply:
python ingest.py
```

This will:
- Create 19 normalized tables (billing_document_headers, products, business_partners, etc.)
- Load ~50K+ rows from JSONL files into Supabase
- Create all necessary indexes

### 2. ⚠️ ChromaDB Cloud Config Mismatch (FIXED ✅)
**Issue:** .env had `CHROMA_TENANT` but code looked for `CHROMA_TENANT_ID`

**Fixed in:**
- ✅ search/semantic.py (already fixed)
- ✅ migrate_to_cloud.py (just fixed)
- ✅ verify_chromadb.py (just fixed)

### 3. ✅ Database Configuration (FIXED ✅)
**Was:** DB_HOST pointing to Render Postgres  
**Now:** DB_HOST points to Supabase (for both local & production)

Both local development and Render production use **Supabase** via `DATABASE_URL`.

---

## Architecture

```
Local Development:      DATABASE_URL (Supabase) ← .env
              ↓
Render Production:      DATABASE_URL (Supabase) ← inherited from .env
              ↓
       Query Execution: db_executor.py uses DATABASE_URL
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

# 3. Ingest SAP data into Supabase
python ingest.py

# 4. Verify tables were created
# (diagnostic.py will show them)
python diagnostic.py
```

### ✅ Phase 2: Verify Queries Work Locally

```bash
# Start server
uvicorn main:app --reload --port 8000

# In another terminal, test a query
curl -X POST http://localhost:8000/query/sync \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me the top 5 customers by revenue"}'

# Should return data from Supabase ✅
```

### ✅ Phase 3: Deploy to Render

1. **Commit changes:**
   ```bash
   git add .env diagnostic.py PRODUCTION_FIX_GUIDE.md migrate_to_cloud.py verify_chromadb.py
   git commit -m "Fix: Use Supabase for production, fix ChromaDB config"
   git push
   ```

2. **Render will auto-deploy** with same `.env` configuration (Supabase)

3. **Verify Render environment variables are set:**
   - Go to Render dashboard → Your service → Environment
   - Add these 4 vars:
   ```
   CHROMA_API_KEY=ck-Atoibaf78hdHaZQBrbME3ge4VKLBWBhWJH1dQYCoEERo
   CHROMA_TENANT=b5a4c0a6-9a4d-4560-a66e-baaab5fd8546
   CHROMA_DATABASE=dodgeai-o2c
   CHROMA_USE_CLOUD=true
   ```
   - REDIS_URL and DATABASE_URL are already inherited from .env
   - Click "Save changes"

4. **Test production endpoint:**
   ```bash
   curl -X POST https://your-render-url/query/sync \
     -H "Content-Type: application/json" \
     -d '{"query": "Show me the top 5 customers by revenue"}'
   ```

---

## Your Configuration

| Component | Local | Production (Render) |
|-----------|-------|-------------------|
| Database | Supabase | Supabase (same) |
| ChromaDB | Cloud | Cloud |
| Redis | Remote | Remote (same) |
| Entry Point | localhost:8000 | render.com/... |

---

## Troubleshooting

### If you still get "no such table" errors:

```bash
# Check if tables were created
python diagnostic.py

# If tables are missing, ingest data:
python ingest.py

# For Render, you can also SSH in:
# Then run: python ingest.py
```

### If ChromaDB Cloud still fails:

```bash
# Verify locally
python verify_chromadb.py

# If works locally but not on Render:
# → Check Render environment variables are set
# → Restart the service in Render dashboard
```

---

## Current Status

| Component | Status | Action |
|-----------|--------|--------|
| ChromaDB Config | ✅ Fixed | Commit changes |
| Database Config | ✅ Fixed | Use Supabase (both local & prod) |
| Database Ingestion | ❌ TODO | Run `python ingest.py` |
| Render Env Vars | ⚠️ TODO | Add 4 vars (ChromaDB) to Render |
| Code Deployed | ✅ Ready | Push to git |

---

## Next Steps

1. **Immediately:** Run `python ingest.py` to load SAP data into Supabase
2. **Then:** Test locally with `uvicorn main:app --reload --port 8000`
3. **Then:** Commit and push fixes
4. **Finally:** Add 4 ChromaDB vars to Render environment
