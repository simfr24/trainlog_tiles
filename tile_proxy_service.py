# tile_proxy_service.py
import asyncio
import aiohttp
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import os
import json
from typing import Optional
import logging
from contextlib import asynccontextmanager
import time
import traceback

# Setup detailed logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logger.info("Starting tile proxy service...")

# Global variables for shared resources
redis_client = None
http_session = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    global redis_client, http_session
    
    logger.info("Starting application startup...")
    
    try:
        # Startup - Redis connection with retry logic
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        logger.info(f"Connecting to Redis at: {redis_url}")
        
        max_retries = 5
        for attempt in range(max_retries):
            try:
                redis_client = redis.from_url(
                    redis_url,
                    decode_responses=False,
                    socket_connect_timeout=5,
                    socket_timeout=5
                )
                # Test connection
                await redis_client.ping()
                logger.info("Redis connection successful")
                break
            except Exception as e:
                logger.warning(f"Redis connection attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt == max_retries - 1:
                    logger.error("Could not connect to Redis, continuing without cache")
                    redis_client = None
                else:
                    await asyncio.sleep(2)
        
        # HTTP session with connection pooling
        logger.info("Setting up HTTP session...")
        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=20,
            keepalive_timeout=30
        )
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        http_session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={'User-Agent': 'TileProxy/2.0'}
        )
        
        logger.info("Tile proxy service started successfully")
        
    except Exception as e:
        logger.error(f"Failed to start services: {e}")
        logger.error(traceback.format_exc())
        raise
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    try:
        if http_session:
            await http_session.close()
        if redis_client:
            await redis_client.close()
        logger.info("Tile proxy service stopped")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")

app = FastAPI(
    title="Tile Proxy Service",
    description="High-performance tile proxy with caching",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*",
        "http://localhost:*",
        "https://localhost:*", 
        "http://127.0.0.1:*",
        "https://127.0.0.1:*", 
        "https://*.trainlog.me",
        "http://*.trainlog.me"
    ],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)

class TileConfig:
    """Configuration management"""
    def __init__(self):
        self.jawg_key = os.getenv("JAWG_API_KEY", "")
        self.thunderforest_key = os.getenv("THUNDERFOREST_API_KEY", "")
        self.cache_ttl = int(os.getenv("CACHE_TTL", "864000"))  # 1 hour default
        self.max_tile_size = int(os.getenv("MAX_TILE_SIZE", "1048576"))  # 1MB default

config = TileConfig()

def build_tile_url(style: str, x: int, y: int, z: int, r: str = "@1x") -> Optional[str]:
    """Build tile URL based on style"""
    
    # URL templates
    jawg_url = f"https://tile.jawg.io/{style}/{z}/{x}/{y}{r}.png?access-token={config.jawg_key}"
    thunderforest_url = f"https://tile.thunderforest.com/transport/{z}/{x}/{y}.png?apikey={config.thunderforest_key}"
    
    style_mapping = {
        "jawg-streets": jawg_url,
        "jawg-lagoon": jawg_url,
        "jawg-sunny": jawg_url,
        "jawg-light": jawg_url,
        "jawg-terrain": jawg_url,
        "jawg-dark": jawg_url,
        "thunderforest-transport": thunderforest_url,
    }
    
    return style_mapping.get(style)

async def get_cached_tile(cache_key: str) -> Optional[bytes]:
    """Get tile from Redis cache"""
    if not redis_client:
        return None
        
    try:
        cached_data = await redis_client.get(cache_key)
        if cached_data:
            logger.info(f"Cache hit: {cache_key}")
            return cached_data
    except Exception as e:
        logger.error(f"Cache read error: {e}")
    return None

async def cache_tile(cache_key: str, tile_data: bytes, ttl: int = None):
    """Cache tile in Redis"""
    if not redis_client:
        return
        
    try:
        ttl = ttl or config.cache_ttl
        await redis_client.setex(cache_key, ttl, tile_data)
        logger.info(f"Cached tile: {cache_key} (TTL: {ttl}s)")
    except Exception as e:
        logger.error(f"Cache write error: {e}")

async def fetch_tile_from_source(url: str) -> tuple[int, Optional[bytes]]:
    """Fetch tile from external source"""
    try:
        async with http_session.get(url) as response:
            if response.status == 200:
                content = await response.read()
                
                # Validate tile size
                if len(content) > config.max_tile_size:
                    logger.warning(f"Tile too large: {len(content)} bytes")
                    return 413, None  # Payload too large
                    
                return 200, content
            else:
                logger.warning(f"Upstream error: {response.status} for {url}")
                return response.status, None
                
    except asyncio.TimeoutError:
        logger.error(f"Timeout fetching: {url}")
        return 504, None
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return 500, None

@app.get("/tile/{style}/{x}/{y}/{z}")
@app.get("/tile/{style}/{x}/{y}/{z}/{r}")
async def get_tile(
    style: str, 
    z: int, 
    x: int, 
    y: int, 
    r: str = "@1x"
):
    """Get tile with caching"""
    
    # Validate zoom level (prevent abuse)
    if not (0 <= z <= 20):
        raise HTTPException(status_code=400, detail="Invalid zoom level")
    
    # Create cache key
    cache_key = f"tile:{style}:{z}:{x}:{y}:{r}"
    
    # Try cache first
    cached_tile = await get_cached_tile(cache_key)
    if cached_tile:
        return Response(
            content=cached_tile,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=864000",
                "X-Cache": "HIT",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "*"
            }
        )
    
    # Build source URL
    tile_url = build_tile_url(style, x, y, z, r)
    if not tile_url:
        raise HTTPException(status_code=400, detail="Unknown tile style")
    
    # Fetch from source
    status_code, tile_data = await fetch_tile_from_source(tile_url)
    
    if status_code == 200 and tile_data:
        # Cache the tile
        await cache_tile(cache_key, tile_data)
        
        return Response(
            content=tile_data,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=864000",
                "X-Cache": "MISS",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "*"
            }
        )
    else:
        raise HTTPException(
            status_code=status_code or 404,
            detail=f"Could not fetch tile for style {style}"
        )

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    health_status = {
        "status": "healthy",
        "timestamp": time.time(),
        "cache": "disconnected",
        "environment": {
            "redis_url": os.getenv("REDIS_URL", "redis://redis:6379/0"),
            "has_jawg_key": bool(os.getenv("JAWG_API_KEY")),
            "has_thunderforest_key": bool(os.getenv("THUNDERFOREST_API_KEY"))
        }
    }
    
    try:
        if redis_client:
            await redis_client.ping()
            health_status["cache"] = "connected"
    except Exception as e:
        logger.warning(f"Redis health check failed: {e}")
        health_status["cache"] = f"error: {str(e)}"
    
    return health_status

@app.get("/cache/stats")
async def cache_stats():
    """Cache statistics"""
    try:
        info = await redis_client.info()
        return {
            "redis_version": info.get("redis_version"),
            "used_memory_human": info.get("used_memory_human"),
            "connected_clients": info.get("connected_clients"),
            "total_commands_processed": info.get("total_commands_processed"),
            "keyspace_hits": info.get("keyspace_hits", 0),
            "keyspace_misses": info.get("keyspace_misses", 0)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/cache/clear")
async def clear_cache():
    """Clear tile cache (admin endpoint)"""
    try:
        # Only clear tile keys
        keys = await redis_client.keys("tile:*")
        if keys:
            await redis_client.delete(*keys)
            return {"message": f"Cleared {len(keys)} cached tiles"}
        return {"message": "No tiles to clear"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# For development
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "tile_proxy_service:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        access_log=True
    )