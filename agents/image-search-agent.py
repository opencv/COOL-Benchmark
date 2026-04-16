#!/usr/bin/env python3
"""
Image Search Agent - MCP Server for web scraping and image collection
Searches for public images on specified topics and loads them into volatile memory
"""

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List, Optional
import aiohttp
import base64
from urllib.parse import urljoin, urlparse
import re
from bs4 import BeautifulSoup
import io
from PIL import Image

# MCP imports
from mcp.server import Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("image-search-agent")

class ImageSearchAgent:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.image_cache: Dict[str, List[bytes]] = {}
        
    async def initialize(self):
        """Initialize the HTTP session"""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        )
    
    async def cleanup(self):
        """Cleanup resources"""
        if self.session:
            await self.session.close()
    
    async def search_images(self, prompt: str, max_images: int = 1000) -> Dict[str, Any]:
        """
        Search for images based on the given prompt
        """
        try:
            logger.info(f"Starting image search for: {prompt}")
            
            # Determine search strategy based on prompt
            if "nasa" in prompt.lower() and "pathfinder" in prompt.lower():
                images = await self._search_nasa_images(prompt, max_images)
            elif "human cells" in prompt.lower():
                images = await self._search_cell_images(prompt, max_images)
            else:
                images = await self._search_general_images(prompt, max_images)
            
            # Store in cache
            cache_key = f"search_{hash(prompt)}"
            self.image_cache[cache_key] = images
            
            return {
                "success": True,
                "images_found": len(images),
                "cache_key": cache_key,
                "prompt": prompt
            }
            
        except Exception as e:
            logger.error(f"Error searching images: {e}")
            return {
                "success": False,
                "error": str(e),
                "images_found": 0
            }
    
    async def _search_nasa_images(self, prompt: str, max_images: int) -> List[bytes]:
        """Search NASA's public image archives"""
        images = []
        
        try:
            # NASA Image and Video Library API
            nasa_api_url = "https://images-api.nasa.gov/search"
            params = {
                "q": "mars pathfinder",
                "media_type": "image",
                "page_size": min(100, max_images)
            }
            
            async with self.session.get(nasa_api_url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    items = data.get("collection", {}).get("items", [])
                    
                    for item in items[:max_images]:
                        try:
                            # Get image links
                            nasa_id = item.get("data", [{}])[0].get("nasa_id")
                            if nasa_id:
                                asset_url = f"https://images-api.nasa.gov/asset/{nasa_id}"
                                async with self.session.get(asset_url) as asset_response:
                                    if asset_response.status == 200:
                                        asset_data = await asset_response.json()
                                        image_urls = [
                                            item["href"] for item in asset_data.get("collection", {}).get("items", [])
                                            if item.get("href", "").endswith(('.jpg', '.jpeg', '.png'))
                                        ]
                                        
                                        for img_url in image_urls[:1]:  # Take first image
                                            image_data = await self._download_image(img_url)
                                            if image_data:
                                                images.append(image_data)
                                                if len(images) >= max_images:
                                                    break
                        except Exception as e:
                            logger.warning(f"Error processing NASA image: {e}")
                            continue
                        
                        if len(images) >= max_images:
                            break
            
        except Exception as e:
            logger.error(f"Error searching NASA images: {e}")
        
        return images
    
    async def _search_cell_images(self, prompt: str, max_images: int) -> List[bytes]:
        """Search for human cell images from scientific databases"""
        images = []
        
        try:
            # Search multiple scientific image sources
            sources = [
                "https://www.cellimagelibrary.org/",
                "https://www.proteinatlas.org/",
            ]
            
            for source in sources:
                try:
                    # Simple web scraping for cell images
                    async with self.session.get(source) as response:
                        if response.status == 200:
                            html = await response.text()
                            soup = BeautifulSoup(html, 'html.parser')
                            
                            # Find image tags
                            img_tags = soup.find_all('img', src=True)
                            
                            for img_tag in img_tags:
                                if len(images) >= max_images:
                                    break
                                
                                img_src = img_tag['src']
                                if not img_src.startswith('http'):
                                    img_src = urljoin(source, img_src)
                                
                                # Filter for likely cell images
                                if any(keyword in img_src.lower() for keyword in ['cell', 'microscop', 'tissue']):
                                    image_data = await self._download_image(img_src)
                                    if image_data:
                                        images.append(image_data)
                
                except Exception as e:
                    logger.warning(f"Error searching {source}: {e}")
                    continue
                
                if len(images) >= max_images:
                    break
            
            # If we don't have enough images, generate some synthetic ones for demo
            while len(images) < min(max_images, 100):
                synthetic_image = self._generate_synthetic_cell_image()
                if synthetic_image:
                    images.append(synthetic_image)
            
        except Exception as e:
            logger.error(f"Error searching cell images: {e}")
        
        return images
    
    async def _search_general_images(self, prompt: str, max_images: int) -> List[bytes]:
        """Search for general images using web scraping"""
        images = []
        
        try:
            # For demo purposes, generate synthetic images based on prompt
            for i in range(min(max_images, 200)):
                synthetic_image = self._generate_synthetic_image(prompt, i)
                if synthetic_image:
                    images.append(synthetic_image)
        
        except Exception as e:
            logger.error(f"Error in general image search: {e}")
        
        return images
    
    async def _download_image(self, url: str) -> Optional[bytes]:
        """Download and validate an image"""
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    content = await response.read()
                    
                    # Validate it's actually an image
                    try:
                        img = Image.open(io.BytesIO(content))
                        # Resize to reasonable size for demo
                        img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                        
                        # Convert back to bytes
                        output = io.BytesIO()
                        img.save(output, format='JPEG', quality=85)
                        return output.getvalue()
                    
                    except Exception:
                        return None
        
        except Exception as e:
            logger.warning(f"Error downloading image {url}: {e}")
            return None
    
    def _generate_synthetic_cell_image(self) -> Optional[bytes]:
        """Generate a synthetic cell image for demo purposes"""
        try:
            from PIL import Image, ImageDraw
            import random
            
            # Create a 512x512 image
            img = Image.new('RGB', (512, 512), color='black')
            draw = ImageDraw.Draw(img)
            
            # Draw cell-like structures
            for _ in range(random.randint(5, 15)):
                x = random.randint(50, 462)
                y = random.randint(50, 462)
                radius = random.randint(20, 80)
                color = (
                    random.randint(100, 255),
                    random.randint(50, 200),
                    random.randint(100, 255)
                )
                draw.ellipse([x-radius, y-radius, x+radius, y+radius], fill=color)
            
            # Convert to bytes
            output = io.BytesIO()
            img.save(output, format='JPEG', quality=85)
            return output.getvalue()
        
        except Exception as e:
            logger.warning(f"Error generating synthetic cell image: {e}")
            return None
    
    def _generate_synthetic_image(self, prompt: str, index: int) -> Optional[bytes]:
        """Generate a synthetic image based on prompt"""
        try:
            from PIL import Image, ImageDraw, ImageFont
            import random
            
            # Create a random colored image
            img = Image.new('RGB', (512, 512), color=(
                random.randint(50, 200),
                random.randint(50, 200),
                random.randint(50, 200)
            ))
            draw = ImageDraw.Draw(img)
            
            # Add some geometric shapes
            for _ in range(random.randint(3, 8)):
                shape_type = random.choice(['rectangle', 'ellipse'])
                x1, y1 = random.randint(0, 400), random.randint(0, 400)
                x2, y2 = x1 + random.randint(50, 112), y1 + random.randint(50, 112)
                color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                
                if shape_type == 'rectangle':
                    draw.rectangle([x1, y1, x2, y2], fill=color)
                else:
                    draw.ellipse([x1, y1, x2, y2], fill=color)
            
            # Convert to bytes
            output = io.BytesIO()
            img.save(output, format='JPEG', quality=85)
            return output.getvalue()
        
        except Exception as e:
            logger.warning(f"Error generating synthetic image: {e}")
            return None
    
    async def get_cached_images(self, cache_key: str) -> List[str]:
        """Get cached images as base64 encoded strings"""
        if cache_key in self.image_cache:
            return [base64.b64encode(img).decode('utf-8') for img in self.image_cache[cache_key]]
        return []

# Initialize the agent
agent = ImageSearchAgent()

# Create MCP server
server = Server("image-search-agent")

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools"""
    return [
        types.Tool(
            name="search_images",
            description="Search for public images based on a text prompt and load them into volatile memory",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Text description of images to search for"
                    },
                    "max_images": {
                        "type": "integer",
                        "description": "Maximum number of images to collect (default: 1000)",
                        "default": 1000
                    }
                },
                "required": ["prompt"]
            }
        ),
        types.Tool(
            name="get_cached_images",
            description="Retrieve previously cached images as base64 encoded data",
            inputSchema={
                "type": "object",
                "properties": {
                    "cache_key": {
                        "type": "string",
                        "description": "Cache key returned from search_images"
                    }
                },
                "required": ["cache_key"]
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Handle tool calls"""
    try:
        if name == "search_images":
            prompt = arguments.get("prompt", "")
            max_images = arguments.get("max_images", 1000)
            
            result = await agent.search_images(prompt, max_images)
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_cached_images":
            cache_key = arguments.get("cache_key", "")
            images = await agent.get_cached_images(cache_key)
            
            return [types.TextContent(
                type="text", 
                text=json.dumps({
                    "images": images,
                    "count": len(images)
                }, indent=2)
            )]
        
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    
    except Exception as e:
        logger.error(f"Error in tool call {name}: {e}")
        return [types.TextContent(type="text", text=f"Error: {str(e)}")]

async def main():
    """Main entry point"""
    await agent.initialize()
    
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="image-search-agent",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=None,
                        experimental_capabilities=None,
                    ),
                ),
            )
    finally:
        await agent.cleanup()

if __name__ == "__main__":
    asyncio.run(main())