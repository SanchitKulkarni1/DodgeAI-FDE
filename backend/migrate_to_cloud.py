"""
migrate_to_cloud.py — Migrate local ChromaDB embeddings to ChromaDB Cloud

This script:
1. Connects to local ChromaDB (chroma_store/)
2. Reads all collections and documents
3. Connects to ChromaDB Cloud
4. Pushes all docs with embeddings to cloud
5. Verifies migration success

Usage:
    python migrate_to_cloud.py
"""

import logging
import sys
from pathlib import Path
from tqdm import tqdm
from dotenv import load_dotenv
import os

import chromadb
from search.semantic import GeminiEmbeddingFunction

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# ─────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────

LOCAL_CHROMA_PATH = "./chroma_store"

# ChromaDB Cloud credentials
CHROMA_API_KEY = os.getenv("CHROMA_API_KEY")
CHROMA_TENANT_ID = os.getenv("CHROMA_TENANT")  # Note: .env uses CHROMA_TENANT
CHROMA_DATABASE = os.getenv("CHROMA_DATABASE", "dodgeai-o2c")

# Validate credentials
if not CHROMA_API_KEY or not CHROMA_TENANT_ID:
    log.error("❌ Missing ChromaDB Cloud credentials in .env")
    log.error("   Required: CHROMA_API_KEY, CHROMA_TENANT_ID")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────
# Migration functions
# ─────────────────────────────────────────────────────────────────────────

def connect_local_chroma() -> chromadb.Client:
    """Connect to local ChromaDB."""
    try:
        client = chromadb.PersistentClient(path=LOCAL_CHROMA_PATH)
        log.info(f"✅ Connected to local ChromaDB at {LOCAL_CHROMA_PATH}")
        return client
    except Exception as e:
        log.error(f"❌ Failed to connect to local ChromaDB: {e}")
        sys.exit(1)


def connect_cloud_chroma() -> chromadb.Client:
    """Connect to ChromaDB Cloud."""
    try:
        client = chromadb.CloudClient(
            api_key=CHROMA_API_KEY,
            tenant=CHROMA_TENANT_ID,
            database=CHROMA_DATABASE,
        )
        log.info(f"✅ Connected to ChromaDB Cloud (tenant={CHROMA_TENANT_ID[:8]}...)")
        return client
    except Exception as e:
        log.error(f"❌ Failed to connect to ChromaDB Cloud: {e}")
        sys.exit(1)


def get_all_collections_and_docs(local_client: chromadb.Client) -> dict:
    """Get all collections and their documents from local ChromaDB."""
    collections_data = {}
    
    try:
        collections = local_client.list_collections()
        log.info(f"📦 Found {len(collections)} collection(s) locally")
        
        for collection in collections:
            coll_name = collection.name
            coll = local_client.get_collection(name=coll_name)
            count = coll.count()
            
            # Get all documents (ChromaDB doesn't have a limit)
            data = coll.get(include=["embeddings", "documents", "metadatas"])
            
            collections_data[coll_name] = {
                "count": count,
                "ids": data["ids"],
                "documents": data["documents"],
                "embeddings": data["embeddings"],
                "metadatas": data["metadatas"] or [],
            }
            
            log.info(f"  └─ Collection '{coll_name}': {count} documents")
        
        return collections_data
    except Exception as e:
        log.error(f"❌ Failed to read local collections: {e}")
        sys.exit(1)


def push_to_cloud(cloud_client: chromadb.Client, collections_data: dict) -> None:
    """Push collections and documents to ChromaDB Cloud."""
    embed_fn = GeminiEmbeddingFunction()
    
    for coll_name, data in tqdm(collections_data.items(), desc="Pushing collections"):
        try:
            # Delete existing collection in cloud if it exists
            try:
                cloud_client.delete_collection(coll_name)
                log.info(f"  └─ Deleted existing cloud collection '{coll_name}'")
            except (ValueError, Exception):
                # Collection doesn't exist or error deleting; that's ok
                pass
            
            # Create collection with embedding function
            cloud_collection = cloud_client.create_collection(
                name=coll_name,
                embedding_function=embed_fn,
                metadata={"hnsw:space": "cosine"},
            )
            log.info(f"  └─ Created cloud collection '{coll_name}'")
            
            # Push documents in batches (ChromaDB Cloud recommends smaller batches)
            batch_size = 50  # Smaller batch size to avoid issues
            ids = data["ids"]
            documents = data["documents"]
            embeddings = data["embeddings"]
            metadatas = data["metadatas"]
            
            with tqdm(
                total=len(ids),
                desc=f"  └─ Pushing {len(ids)} docs to '{coll_name}'",
                leave=False
            ) as pbar:
                for i in range(0, len(ids), batch_size):
                    batch_ids = ids[i:i+batch_size]
                    batch_docs = documents[i:i+batch_size]
                    batch_embeddings = None
                    batch_metadatas = None
                    
                    # Only include embeddings if they exist and have content
                    if embeddings is not None:
                        try:
                            embedding_len = len(embeddings)
                            if embedding_len > 0:
                                batch_embeddings = []
                                for j in range(i, min(i+batch_size, embedding_len)):
                                    emb = embeddings[j]
                                    # Ensure embedding is a list of floats
                                    if emb is not None:
                                        if hasattr(emb, 'tolist'):
                                            # numpy array
                                            batch_embeddings.append(emb.tolist())
                                        elif isinstance(emb, list):
                                            batch_embeddings.append(emb)
                                        else:
                                            batch_embeddings.append(list(emb) if hasattr(emb, '__iter__') else [0.0] * 3072)
                                    else:
                                        batch_embeddings.append([0.0] * 3072)
                        except (TypeError, ValueError):
                            batch_embeddings = None
                    
                    # Include metadatas if they exist
                    if metadatas is not None:
                        try:
                            if len(metadatas) > 0:
                                batch_metadatas = metadatas[i:i+batch_size]
                        except TypeError:
                            batch_metadatas = None
                    
                    # Add to cloud collection
                    if batch_embeddings is not None and len(batch_embeddings) > 0:
                        cloud_collection.add(
                            ids=batch_ids,
                            documents=batch_docs,
                            embeddings=batch_embeddings,  # Push pre-computed embeddings
                            metadatas=batch_metadatas,
                        )
                    else:
                        # If no embeddings, let ChromaDB compute them
                        cloud_collection.add(
                            ids=batch_ids,
                            documents=batch_docs,
                            metadatas=batch_metadatas,
                        )
                    pbar.update(len(batch_ids))
            
            # Verify count
            cloud_count = cloud_collection.count()
            log.info(
                f"  ✅ Collection '{coll_name}' pushed successfully "
                f"({cloud_count} docs in cloud)"
            )
            
        except Exception as e:
            log.error(f"❌ Failed to push collection '{coll_name}': {e}")
            import traceback
            traceback.print_exc()
            raise


def verify_migration(local_client: chromadb.Client, cloud_client: chromadb.Client) -> None:
    """Verify that migration was successful."""
    log.info("\n🔍 Verifying migration...")
    
    local_collections = {c.name: c.count() for c in local_client.list_collections()}
    cloud_collections = {c.name: c.count() for c in cloud_client.list_collections()}
    
    all_match = True
    for coll_name, local_count in local_collections.items():
        cloud_count = cloud_collections.get(coll_name, 0)
        if local_count == cloud_count:
            log.info(f"  ✅ {coll_name}: {local_count} docs (matches)")
        else:
            log.error(
                f"  ❌ {coll_name}: local={local_count}, cloud={cloud_count} (MISMATCH)"
            )
            all_match = False
    
    if all_match:
        log.info("\n✅ Migration successful! All collections verified.\n")
    else:
        log.error("\n❌ Migration verification failed. Please review mismatches.\n")
        sys.exit(1)


def main():
    """Run migration."""
    log.info("=" * 70)
    log.info("ChromaDB Local → Cloud Migration")
    log.info("=" * 70)
    
    # 1. Connect to local ChromaDB
    log.info("\n1️⃣  Connecting to local ChromaDB...")
    local_client = connect_local_chroma()
    
    # 2. Read all local data
    log.info("\n2️⃣  Reading local collections and documents...")
    collections_data = get_all_collections_and_docs(local_client)
    
    if not collections_data:
        log.warning("⚠️  No collections found locally. Nothing to migrate.")
        return
    
    # 3. Connect to cloud
    log.info("\n3️⃣  Connecting to ChromaDB Cloud...")
    cloud_client = connect_cloud_chroma()
    
    # 4. Push to cloud
    log.info("\n4️⃣  Pushing collections to ChromaDB Cloud...")
    try:
        push_to_cloud(cloud_client, collections_data)
    except Exception as e:
        log.error(f"\n❌ Migration failed: {e}")
        sys.exit(1)
    
    # 5. Verify
    verify_migration(local_client, cloud_client)
    
    log.info("=" * 70)
    log.info("🎉 Migration complete!")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
