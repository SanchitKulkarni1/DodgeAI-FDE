#!/usr/bin/env python3
"""
verify_chromadb.py — Verify ChromaDB Cloud connection and collection existence
"""

import os
import sys
from dotenv import load_dotenv
import chromadb

load_dotenv()

# Get credentials
API_KEY = os.getenv("CHROMA_API_KEY")
TENANT_ID = os.getenv("CHROMA_TENANT_ID")
DATABASE = os.getenv("CHROMA_DATABASE", "dodgeai-o2c")
USE_CLOUD = os.getenv("CHROMA_USE_CLOUD", "true").lower() == "true"

print("=" * 70)
print("ChromaDB Cloud Verification")
print("=" * 70)

print(f"\n📋 Configuration:")
print(f"   CHROMA_USE_CLOUD: {USE_CLOUD}")
print(f"   CHROMA_API_KEY: {API_KEY[:20]}..." if API_KEY else "   CHROMA_API_KEY: ❌ NOT SET")
print(f"   CHROMA_TENANT_ID: {TENANT_ID[:20]}..." if TENANT_ID else "   CHROMA_TENANT_ID: ❌ NOT SET")
print(f"   CHROMA_DATABASE: {DATABASE}")

if not API_KEY or not TENANT_ID:
    print("\n❌ Missing credentials in .env!")
    sys.exit(1)

print(f"\n🔗 Connecting to ChromaDB Cloud...")
try:
    client = chromadb.CloudClient(
        api_key=API_KEY,
        tenant=TENANT_ID,
        database=DATABASE,
    )
    print(f"✅ Connected successfully!")
except Exception as e:
    print(f"❌ Connection failed: {e}")
    sys.exit(1)

print(f"\n📦 Checking collections...")
try:
    collections = client.list_collections()
    print(f"✅ Found {len(collections)} collection(s):")
    for c in collections:
        try:
            count = client.get_collection(c.name).count()
            print(f"   • {c.name}: {count} documents")
        except Exception as e:
            print(f"   • {c.name}: ❌ Error: {e}")
    
    # Specific check for o2c_entities
    print(f"\n🎯 Looking for 'o2c_entities' collection...")
    try:
        o2c_coll = client.get_collection("o2c_entities")
        doc_count = o2c_coll.count()
        print(f"✅ Found 'o2c_entities' with {doc_count} documents!")
    except ValueError as e:
        print(f"❌ 'o2c_entities' not found: {e}")
        print(f"\n💡 Solution:")
        print(f"   1. Run: python migrate_to_cloud.py")
        print(f"   2. This will push your 510 local documents to ChromaDB Cloud")
        
except Exception as e:
    print(f"❌ Error listing collections: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 70)
