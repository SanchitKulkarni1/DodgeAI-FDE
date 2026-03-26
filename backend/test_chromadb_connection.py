#!/usr/bin/env python3
"""
test_chromadb_connection.py — Detailed ChromaDB Cloud connection diagnostics
"""

import os
import sys
import logging
from dotenv import load_dotenv

# Setup detailed logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

print("\n" + "="*80)
print("🔍 CHRMADB CLOUD CONNECTION DETAILED DIAGNOSTICS")
print("="*80)

# ──────────────────────────────────────────────────────────────────────────────
# 1. Check environment variables
# ──────────────────────────────────────────────────────────────────────────────
print("\n📋 ENVIRONMENT VARIABLES:")
print("-" * 80)

api_key = os.getenv("CHROMA_API_KEY")
tenant_id = os.getenv("CHROMA_TENANT")
database = os.getenv("CHROMA_DATABASE", "dodgeai-o2c")
use_cloud = os.getenv("CHROMA_USE_CLOUD", "false").lower() == "true"

print(f"CHROMA_USE_CLOUD:   {use_cloud}")
print(f"CHROMA_API_KEY:     {api_key[:20]}..." if api_key else "CHROMA_API_KEY:     ❌ NOT SET")
print(f"CHROMA_TENANT:      {tenant_id[:20]}..." if tenant_id else "CHROMA_TENANT:      ❌ NOT SET")
print(f"CHROMA_DATABASE:    {database}")

# ──────────────────────────────────────────────────────────────────────────────
# 2. Validate format
# ──────────────────────────────────────────────────────────────────────────────
print("\n✅ VALIDATION:")
print("-" * 80)

issues = []

if not api_key:
    print("❌ CHROMA_API_KEY is missing!")
    issues.append("API_KEY missing")
else:
    if api_key.startswith("ck-"):
        print(f"✅ API_KEY format looks correct (ck-...)")
    else:
        print(f"⚠️  API_KEY doesn't start with 'ck-': {api_key[:10]}")
        issues.append("API_KEY format wrong")

if not tenant_id:
    print("❌ CHROMA_TENANT is missing!")
    issues.append("TENANT missing")
else:
    if len(tenant_id) == 36 and tenant_id.count("-") == 4:
        print(f"✅ TENANT format looks correct (UUID: {tenant_id})")
    else:
        print(f"⚠️  TENANT doesn't look like a UUID: {tenant_id}")
        issues.append("TENANT format wrong")

if database:
    print(f"✅ DATABASE is set: {database}")
else:
    print("❌ DATABASE is missing!")
    issues.append("DATABASE missing")

# ──────────────────────────────────────────────────────────────────────────────
# 3. Try connection
# ──────────────────────────────────────────────────────────────────────────────
print("\n🔗 CONNECTION TEST:")
print("-" * 80)

if issues:
    print(f"\n❌ Cannot test connection: {len(issues)} issue(s) found")
    for issue in issues:
        print(f"   • {issue}")
    sys.exit(1)

try:
    import chromadb
    
    logger.debug("Importing chromadb CloudClient...")
    print("Attempting to connect...")
    print(f"  api_key:  {api_key[:30]}...")
    print(f"  tenant:   {tenant_id}")
    print(f"  database: {database}")
    
    client = chromadb.CloudClient(
        api_key=api_key,
        tenant=tenant_id,
        database=database,
    )
    
    print("✅ Connected successfully!")
    
    # Try to list collections
    print("\n📦 Collections:")
    collections = client.list_collections()
    if collections:
        for c in collections:
            print(f"  • {c.name}")
            try:
                count = client.get_collection(c.name).count()
                print(f"    └─ {count} documents")
            except Exception as e:
                print(f"    └─ Error counting: {e}")
    else:
        print("  (no collections yet)")
    
except ImportError:
    print("❌ chromadb not installed")
    sys.exit(1)
except Exception as e:
    print(f"❌ Connection failed: {e}")
    logger.exception(f"Full exception:")
    
    # Diagnose the error
    error_str = str(e).lower()
    if "authenticate" in error_str or "unauthorized" in error_str:
        print("\n💡 Diagnosis: Authentication failed")
        print("   → Check CHROMA_API_KEY is correct")
        print("   → Try regenerating in ChromaDB Cloud dashboard")
    elif "tenant" in error_str or "not found" in error_str:
        print("\n💡 Diagnosis: Tenant not found")
        print("   → Check CHROMA_TENANT is correct")
        print("   → Verify tenant exists in ChromaDB Cloud dashboard")
    elif "connection" in error_str or "network" in error_str:
        print("\n💡 Diagnosis: Network connection issue")
        print("   → Check internet connection")
        print("   → Verify ChromaDB Cloud is accessible (api.trychroma.com)")
    else:
        print(f"\n💡 Diagnosis: Unknown error")
        print("   → Check ChromaDB Cloud status")
        print("   → Review error message carefully")
    
    sys.exit(1)

print("\n" + "="*80)
print("✅ ALL CHECKS PASSED - ChromaDB Cloud is accessible!")
print("="*80)
