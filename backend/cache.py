"""
cache.py — Redis-backed query result caching.

Provides query result caching with TTL to reduce repeated query latency.
Supports:
  - Automatic cache key generation from query + parameters
  - Configurable TTL (Time To Live) per cache entry
  - Manual cache invalidation
  - Graceful fallback if Redis is unavailable

Performance impact:
  - First query: Regular latency (cached for future use)
  - Subsequent identical queries: ~90% latency reduction (cache hit)
  - Cache miss on timeout: Automatic refresh

Installation:
    pip install redis
    
Configuration:
    Set REDIS_URL in .env:
        REDIS_URL=redis://localhost:6379/0
    Or use defaults:
        REDIS_HOST=localhost
        REDIS_PORT=6379
        REDIS_DB=0
"""

import json
import logging
import os
import hashlib
from typing import Optional, Any, List
from decimal import Decimal
from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Load environment variables
load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# JSON Encoder for Decimal types (from PostgreSQL numeric columns)
# ─────────────────────────────────────────────────────────────────────────────

class DecimalEncoder(json.JSONEncoder):
    """Encode Decimal objects as floats for JSON serialization."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)

REDIS_AVAILABLE = False
redis_client: Optional[Any] = None

try:
    import redis
    
    # Try to connect to Redis
    _REDIS_URL = os.getenv("REDIS_URL", "").strip()
    if _REDIS_URL:
        redis_client = redis.from_url(_REDIS_URL, decode_responses=True)
    else:
        # Fall back to individual params
        _REDIS_HOST = os.getenv("REDIS_HOST", "localhost").strip()
        _REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
        _REDIS_DB = int(os.getenv("REDIS_DB", 0))
        
        redis_client = redis.Redis(
            host=_REDIS_HOST,
            port=_REDIS_PORT,
            db=_REDIS_DB,
            decode_responses=True,
        )
    
    # Test connection
    redis_client.ping()
    REDIS_AVAILABLE = True
    log.info("[cache] ✅ Redis connection successful")
    
except Exception as e:
    log.warning("[cache] ⚠️ Redis unavailable — caching disabled: %s", e)
    redis_client = None
    REDIS_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Cache Configuration
# ─────────────────────────────────────────────────────────────────────────────

# TTL in seconds for different query types
CACHE_TTL = {
    "aggregation": 3600,      # 1 hour — aggregations are stable
    "sql":         1800,      # 30 minutes — general queries
    "semantic":    600,       # 10 minutes — semantic results change less frequently
    "hybrid":      1800,      # 30 minutes — hybrid queries
    "default":     1200,      # 20 minutes — default TTL
}

# Cache key prefix to avoid collisions
CACHE_PREFIX = "dodgeai:query:"


# ─────────────────────────────────────────────────────────────────────────────
# Cache Key Generation
# ─────────────────────────────────────────────────────────────────────────────

def _generate_cache_key(
    query: str,
    query_type: str = "default",
    customer_id: Optional[str] = None,
) -> str:
    """
    Generate a cache key from query parameters.
    
    Args:
        query: The SQL query or search query string
        query_type: Type of query (aggregation, sql, semantic, etc.)
        customer_id: Optional customer ID for customer-scoped caching
    
    Returns:
        Cache key suitable for Redis
    """
    # Normalize query (remove extra whitespace)
    normalized_query = " ".join(query.split()).lower()
    
    # Create hash of query for key
    query_hash = hashlib.md5(normalized_query.encode()).hexdigest()[:16]
    
    # Build cache key
    if customer_id:
        cache_key = f"{CACHE_PREFIX}{customer_id}:{query_type}:{query_hash}"
    else:
        cache_key = f"{CACHE_PREFIX}{query_type}:{query_hash}"
    
    return cache_key


# ─────────────────────────────────────────────────────────────────────────────
# Core Caching Functions
# ─────────────────────────────────────────────────────────────────────────────

def get_cached(
    query: str,
    query_type: str = "default",
    customer_id: Optional[str] = None,
) -> Optional[List[dict]]:
    """
    Retrieve cached query results from Redis.
    
    Args:
        query: The SQL query or search query
        query_type: Type of query
        customer_id: Optional customer ID
    
    Returns:
        Cached results (list of dicts) or None if cache miss
    """
    if not REDIS_AVAILABLE or redis_client is None:
        return None
    
    try:
        cache_key = _generate_cache_key(query, query_type, customer_id)
        cached_data = redis_client.get(cache_key)
        
        if cached_data:
            log.debug("[cache] HIT — key=%s", cache_key[:40])
            return json.loads(cached_data)
        else:
            log.debug("[cache] MISS — key=%s", cache_key[:40])
            return None
        
    except Exception as e:
        log.warning("[cache] Error retrieving cached data: %s", e)
        return None


def set_cached(
    query: str,
    results: List[dict],
    query_type: str = "default",
    customer_id: Optional[str] = None,
    ttl: Optional[int] = None,
) -> bool:
    """
    Store query results in Redis cache.
    
    Args:
        query: The SQL query or search query
        results: Results to cache (list of dicts)
        query_type: Type of query
        customer_id: Optional customer ID
        ttl: Cache TTL in seconds (uses defaults if None)
    
    Returns:
        True if cached successfully, False otherwise
    """
    if not REDIS_AVAILABLE or redis_client is None:
        return False
    
    try:
        cache_key = _generate_cache_key(query, query_type, customer_id)
        
        # Determine TTL
        if ttl is None:
            ttl = CACHE_TTL.get(query_type, CACHE_TTL["default"])
        
        # Serialize and store (use DecimalEncoder to handle PostgreSQL Decimal types)
        cache_data = json.dumps(results, cls=DecimalEncoder)
        redis_client.setex(cache_key, ttl, cache_data)
        
        log.debug("[cache] STORED — key=%s ttl=%ds", cache_key[:40], ttl)
        return True
        
    except Exception as e:
        log.warning("[cache] Error storing cached data: %s", e)
        return False


def invalidate_cache(
    query: Optional[str] = None,
    query_type: Optional[str] = None,
    customer_id: Optional[str] = None,
) -> int:
    """
    Invalidate cache entries.
    
    Args:
        query: Specific query to invalidate (if None, invalidates all of type)
        query_type: Query type to invalidate
        customer_id: Customer ID for scoped invalidation
    
    Returns:
        Number of keys deleted
    """
    if not REDIS_AVAILABLE or redis_client is None:
        return 0
    
    try:
        if query:
            # Invalidate specific query
            cache_key = _generate_cache_key(query, query_type or "default", customer_id)
            deleted = redis_client.delete(cache_key)
            log.info("[cache] INVALIDATED query — deleted=%d", deleted)
            return deleted
        else:
            # Invalidate all queries of type/customer
            pattern = f"{CACHE_PREFIX}*"
            if customer_id:
                pattern = f"{CACHE_PREFIX}{customer_id}:*"
            elif query_type:
                pattern = f"{CACHE_PREFIX}{query_type}:*"
            
            # Use SCAN for large datasets (non-blocking)
            keys = []
            for key in redis_client.scan_iter(match=pattern):
                keys.append(key)
            
            if keys:
                deleted = redis_client.delete(*keys)
                log.info("[cache] INVALIDATED pattern=%s — deleted=%d", pattern, deleted)
                return deleted
            return 0
            
    except Exception as e:
        log.warning("[cache] Error invalidating cache: %s", e)
        return 0


def clear_cache() -> bool:
    """
    Clear all DodgeAI cache entries from Redis.
    WARNING: This does NOT clear entire Redis DB, only our keys.
    """
    if not REDIS_AVAILABLE or redis_client is None:
        return False
    
    try:
        deleted = invalidate_cache()
        log.info("[cache] CLEARED all DodgeAI caches — deleted=%d keys", deleted)
        return True
    except Exception as e:
        log.warning("[cache] Error clearing cache: %s", e)
        return False


def get_cache_stats() -> dict:
    """
    Get cache statistics (for monitoring/debugging).
    """
    if not REDIS_AVAILABLE or redis_client is None:
        return {"available": False, "reason": "Redis unavailable"}
    
    try:
        # Count our keys
        pattern = f"{CACHE_PREFIX}*"
        key_count = 0
        for _ in redis_client.scan_iter(match=pattern):
            key_count += 1
        
        # Get Redis info
        info = redis_client.info()
        
        return {
            "available": True,
            "cache_keys": key_count,
            "redis_memory_mb": info.get("used_memory_human", "unknown"),
            "redis_connected_clients": info.get("connected_clients", 0),
        }
    except Exception as e:
        log.warning("[cache] Error getting stats: %s", e)
        return {"available": False, "reason": str(e)}
