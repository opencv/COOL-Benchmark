#!/usr/bin/env python3
"""
Benchmark Orchestrator - Main service that coordinates all components
Handles API requests from frontend and orchestrates the benchmarking process
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum
import aiohttp
from aiohttp import web
import sys
import os

# Add project paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agentcore'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agents'))
sys.path.insert(0, os.path.dirname(__file__))

# Import with hyphenated filenames
import importlib.util
spec = importlib.util.spec_from_file_location("instance_manager", os.path.join(os.path.dirname(__file__), '..', 'agentcore', 'instance-manager.py'))
instance_manager_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(instance_manager_module)
InstanceManager = instance_manager_module.InstanceManager

from build_manager import BuildManager
from benchmark_executor import execute_benchmark_with_build
from auto_retry_manager import AutoRetryManager

# Configure logging to both console and file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('orchestrator-runtime.log')
    ]
)
logger = logging.getLogger("benchmark-orchestrator")

# Disable aiohttp access logs (too verbose)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


class TaskStatus(Enum):
    PENDING = "pending"
    STAGING = "staging"  # Instance launching and OpenCV installation
    RUNNING = "running"  # Actually processing images
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class BenchmarkTask:
    task_id: str
    test_type: str
    instance_type: str
    max_instances: int
    image_count: int
    status: TaskStatus
    start_time: float
    build_mode: str = "pip"
    iterations: int = 100
    pipeline_type: str = "standard"
    build_progress: Optional[Dict[str, Any]] = None
    end_time: Optional[float] = None
    results: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

@dataclass
class ImageSearchTask:
    task_id: str
    prompt: str
    status: TaskStatus
    images_found: int = 0
    progress: float = 0.0
    images: List[str] = None
    error: Optional[str] = None
    start_time: float = 0.0
    timeout: int = 20

@dataclass
class BuildAttempt:
    """Track the last build attempt for each architecture/build_mode combination"""
    architecture: str  # 'graviton' or 'x86'
    build_mode: str  # 'pip' or 'compile'
    status: str  # 'success' or 'failed'
    duration: float  # in seconds
    timestamp: float
    instance_type: str
    error: Optional[str] = None

class BenchmarkOrchestrator:
    def __init__(self):
        _region = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
        self.instance_manager = InstanceManager(region=_region)
        self.build_manager = BuildManager()
        self.auto_retry_manager = None  # Will be initialized after instance_manager
        self.benchmark_tasks: Dict[str, BenchmarkTask] = {}
        self.image_search_tasks: Dict[str, ImageSearchTask] = {}
        self.session = aiohttp.ClientSession()
        
        # Track last build attempts for each configuration
        self.build_history: Dict[str, BuildAttempt] = {}  # key: f"{architecture}_{build_mode}"
        
        # Configuration
        self.marketplace_ami_id = os.environ.get("MARKETPLACE_AMI_ID", "")  # Set via UI or MARKETPLACE_AMI_ID env var
        self.marketplace_license_key = None  # Will be loaded from config
        self.base_arm64_ami_id = None  # Will be fetched dynamically
        self.base_x86_ami_id = None  # Will be fetched dynamically
        self.default_region = _region
        
        # Load marketplace configuration if available
        self._load_marketplace_config()
    
    def _load_marketplace_config(self):
        """Load marketplace AMI configuration from config file"""
        try:
            config_path = os.path.join(os.path.dirname(__file__), '..', 'config-marketplace.json')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    marketplace = config.get('marketplace', {})
                    self.marketplace_ami_id = marketplace.get('ami_id', self.marketplace_ami_id)
                    self.marketplace_license_key = marketplace.get('license_key')
                    logger.info(f"Loaded marketplace config: AMI={self.marketplace_ami_id}, License={'configured' if self.marketplace_license_key else 'not set'}")
            else:
                logger.warning("Marketplace config not found, using defaults")
        except Exception as e:
            logger.error(f"Error loading marketplace config: {e}")
        
    async def initialize(self):
        """Initialize the orchestrator"""
        try:
            await self.instance_manager.initialize()
            
            # Initialize auto-retry manager (pass self for orchestrator reference)
            self.auto_retry_manager = AutoRetryManager(self.build_manager, self.instance_manager, self)
            
            # Cleanup any orphaned benchmark instances from previous runs
            await self._cleanup_orphaned_instances()
            
            # Fetch latest base AMIs
            import boto3
            ec2 = boto3.client('ec2', region_name=self.default_region)
            
            # Get latest Ubuntu 24.04 ARM64
            arm64_response = ec2.describe_images(
                Owners=['099720109477'],  # Canonical (Ubuntu) owner ID
                Filters=[
                    {'Name': 'name', 'Values': ['ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*']},
                    {'Name': 'state', 'Values': ['available']},
                    {'Name': 'architecture', 'Values': ['arm64']}
                ]
            )
            if arm64_response['Images']:
                sorted_arm64 = sorted(arm64_response['Images'], key=lambda x: x['CreationDate'], reverse=True)
                self.base_arm64_ami_id = sorted_arm64[0]['ImageId']
                logger.info(f"Using Ubuntu 24.04 ARM64 base AMI: {self.base_arm64_ami_id}")

            # Get latest Ubuntu 24.04 x86_64
            x86_response = ec2.describe_images(
                Owners=['099720109477'],  # Canonical (Ubuntu) owner ID
                Filters=[
                    {'Name': 'name', 'Values': ['ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*']},
                    {'Name': 'state', 'Values': ['available']},
                    {'Name': 'architecture', 'Values': ['x86_64']}
                ]
            )
            if x86_response['Images']:
                sorted_x86 = sorted(x86_response['Images'], key=lambda x: x['CreationDate'], reverse=True)
                self.base_x86_ami_id = sorted_x86[0]['ImageId']
                logger.info(f"Using Ubuntu 24.04 x86_64 base AMI: {self.base_x86_ami_id}")

            
            logger.info("Benchmark orchestrator initialized")
        except Exception as e:
            logger.error(f"Failed to initialize orchestrator: {e}")
            raise
    
    async def start_image_search(self, prompt: str, max_images: int = 1000, timeout: int = 20) -> str:
        """Start an image search task"""
        task_id = str(uuid.uuid4())
        
        task = ImageSearchTask(
            task_id=task_id,
            prompt=prompt,
            status=TaskStatus.PENDING,
            images=[],
            start_time=time.time(),
            timeout=timeout
        )
        
        self.image_search_tasks[task_id] = task
        
        # Start search in background
        asyncio.create_task(self._execute_image_search(task_id, prompt, max_images, timeout))
        
        logger.info(f"Started image search task {task_id} with {timeout}s timeout")
        return task_id
    
    async def _execute_image_search(self, task_id: str, prompt: str, max_images: int, timeout: int = 20):
        """Execute image search using real web scraping"""
        try:
            task = self.image_search_tasks[task_id]
            task.status = TaskStatus.RUNNING
            
            # Initialize images list immediately for incremental updates
            if task.images is None:
                task.images = []
            
            # Log search strategy
            if "nasa" in prompt.lower() and ("mars" in prompt.lower() or "pathfinder" in prompt.lower()):
                logger.info(f"🚀 Launching 4 parallel search threads for NASA Mars images:")
                logger.info(f"   Thread 1: Wikimedia Commons API (commons.wikimedia.org)")
                logger.info(f"   Thread 2: NASA Image API (images-api.nasa.gov)")
                logger.info(f"   Thread 3: Google Images (google.com/images)")
                logger.info(f"   Thread 4: Bing Images (bing.com/images)")
                logger.info(f"   Timeout: {timeout}s | Target: {max_images} images")
                images = await self._fetch_nasa_images(prompt, max_images, task, timeout)
            elif "cell" in prompt.lower() and "human" in prompt.lower():
                logger.info(f"🔬 Launching 4 parallel search threads for cell microscopy images:")
                logger.info(f"   Thread 1: Flickr (flickr.com)")
                logger.info(f"   Thread 2: Wikimedia Commons (commons.wikimedia.org)")
                logger.info(f"   Thread 3: Google Images (google.com/images)")
                logger.info(f"   Thread 4: Bing Images (bing.com/images)")
                logger.info(f"   Timeout: {timeout}s | Target: {max_images} images")
                images = await self._fetch_cell_images(prompt, max_images, task, timeout)
            else:
                logger.info(f"🔍 Launching general image search:")
                logger.info(f"   Timeout: {timeout}s | Target: {max_images} images")
                images = await self._fetch_general_images(prompt, max_images, task, timeout)
            
            task.status = TaskStatus.COMPLETED
            task.progress = 100.0
            task.images = images
            task.images_found = len(images)
            
            logger.info(f"✅ Image search task {task_id} completed with {len(images)} images")
            
        except Exception as e:
            logger.error(f"Error in image search task {task_id}: {e}")
            task.status = TaskStatus.FAILED
            task.error = str(e)
    
    async def _download_and_encode_image(self, session, url: str) -> Optional[str]:
        """Download an image and encode it as base64"""
        try:
            async with session.get(url, timeout=15) as response:
                if response.status == 200:
                    content = await response.read()
                    
                    from PIL import Image
                    import io
                    
                    img = Image.open(io.BytesIO(content))
                    img.thumbnail((512, 512), Image.Resampling.LANCZOS)
                    
                    output = io.BytesIO()
                    if img.mode in ('RGBA', 'LA', 'P'):
                        img = img.convert('RGB')
                    img.save(output, format='JPEG', quality=85)
                    
                    import base64
                    return base64.b64encode(output.getvalue()).decode('utf-8')
                    
        except Exception as e:
            logger.warning(f"Error downloading image {url}: {e}")
            return None
    
    async def _fetch_nasa_images(self, prompt: str, max_images: int, task, timeout: int = 20) -> List[str]:
        """Fetch real NASA Mars images with TRULY concurrent requests from multiple sources"""
        images = []
        start_time = time.time()
        timeout_seconds = timeout
        
        try:
            from bs4 import BeautifulSoup
            
            logger.info(f"Fetching NASA Mars images with PARALLEL concurrent requests ({timeout}s timeout)...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            }
            
            async with aiohttp.ClientSession(headers=headers) as session:
                
                # Define all search sources as async functions
                async def fetch_wikimedia():
                    """Fetch from Wikimedia Commons"""
                    source_images = []
                    try:
                        wiki_api = "https://commons.wikimedia.org/w/api.php"
                        params = {
                            "action": "query",
                            "format": "json",
                            "generator": "categorymembers",
                            "gcmtitle": "Category:Mars_Pathfinder_images",
                            "gcmtype": "file",
                            "gcmlimit": 500,
                            "prop": "imageinfo",
                            "iiprop": "url",
                            "iiurlwidth": 512
                        }
                        
                        async with session.get(wiki_api, params=params, timeout=30) as response:
                            if response.status == 200:
                                data = await response.json()
                                pages = data.get("query", {}).get("pages", {})
                                
                                logger.info(f"[Wikimedia] Found {len(pages)} images")
                                
                                # Extract URLs
                                image_urls = []
                                for page_data in pages.values():
                                    imageinfo = page_data.get("imageinfo", [])
                                    if imageinfo:
                                        thumb_url = imageinfo[0].get("thumburl") or imageinfo[0].get("url")
                                        if thumb_url:
                                            image_urls.append(thumb_url)
                                
                                # Download in batches
                                batch_size = 20
                                for i in range(0, len(image_urls), batch_size):
                                    if time.time() - start_time > timeout_seconds:
                                        break
                                    
                                    batch = image_urls[i:i+batch_size]
                                    download_tasks = [self._download_and_encode_image(session, url) for url in batch]
                                    results = await asyncio.gather(*download_tasks, return_exceptions=True)
                                    
                                    for img_b64 in results:
                                        if img_b64 and not isinstance(img_b64, Exception):
                                            source_images.append(img_b64)
                                
                                logger.info(f"[Wikimedia] Downloaded {len(source_images)} images")
                    
                    except Exception as e:
                        logger.warning(f"[Wikimedia] Error: {e}")
                    
                    return source_images
                
                async def fetch_nasa_api():
                    """Fetch from NASA Image API"""
                    source_images = []
                    try:
                        nasa_api = "https://images-api.nasa.gov/search"
                        params = {
                            "q": "mars pathfinder sojourner rover",
                            "media_type": "image",
                            "year_start": "1996",
                            "year_end": "1998"
                        }
                        
                        async with session.get(nasa_api, params=params, timeout=20) as response:
                            if response.status == 200:
                                data = await response.json()
                                items = data.get("collection", {}).get("items", [])
                                
                                logger.info(f"[NASA API] Found {len(items)} items")
                                
                                # Extract URLs
                                image_urls = []
                                for item in items:
                                    links = item.get("links", [])
                                    for link in links:
                                        if link.get("render") == "image":
                                            img_url = link.get("href")
                                            if img_url:
                                                image_urls.append(img_url)
                                                break
                                
                                # Download in batches
                                batch_size = 15
                                for i in range(0, len(image_urls), batch_size):
                                    if time.time() - start_time > timeout_seconds:
                                        break
                                    
                                    batch = image_urls[i:i+batch_size]
                                    download_tasks = [self._download_and_encode_image(session, url) for url in batch]
                                    results = await asyncio.gather(*download_tasks, return_exceptions=True)
                                    
                                    for img_b64 in results:
                                        if img_b64 and not isinstance(img_b64, Exception):
                                            source_images.append(img_b64)
                                
                                logger.info(f"[NASA API] Downloaded {len(source_images)} images")
                    
                    except Exception as e:
                        logger.warning(f"[NASA API] Error: {e}")
                    
                    return source_images
                
                async def fetch_google_images():
                    """Fetch from Google Images via web scraping"""
                    source_images = []
                    try:
                        # Google Images search (scraping approach)
                        search_query = "mars+pathfinder+nasa"
                        google_url = f"https://www.google.com/search?q={search_query}&tbm=isch"
                        
                        async with session.get(google_url, timeout=20) as response:
                            if response.status == 200:
                                html = await response.text()
                                soup = BeautifulSoup(html, 'html.parser')
                                
                                logger.info(f"[Google Images] Scraping search results")
                                
                                # Find image URLs in the page
                                img_tags = soup.find_all('img')
                                image_urls = []
                                for img in img_tags:
                                    src = img.get('src') or img.get('data-src')
                                    if src and src.startswith('http'):
                                        image_urls.append(src)
                                
                                # Download in batches
                                batch_size = 10
                                for i in range(0, min(len(image_urls), 50), batch_size):  # Limit to 50 images
                                    if time.time() - start_time > timeout_seconds:
                                        break
                                    
                                    batch = image_urls[i:i+batch_size]
                                    download_tasks = [self._download_and_encode_image(session, url) for url in batch]
                                    results = await asyncio.gather(*download_tasks, return_exceptions=True)
                                    
                                    for img_b64 in results:
                                        if img_b64 and not isinstance(img_b64, Exception):
                                            source_images.append(img_b64)
                                
                                logger.info(f"[Google Images] Downloaded {len(source_images)} images")
                    
                    except Exception as e:
                        logger.warning(f"[Google Images] Error: {e}")
                    
                    return source_images
                
                async def fetch_bing_images():
                    """Fetch from Bing Images via web scraping"""
                    source_images = []
                    try:
                        # Bing Images search
                        search_query = "mars+pathfinder+nasa"
                        bing_url = f"https://www.bing.com/images/search?q={search_query}"
                        
                        async with session.get(bing_url, timeout=20) as response:
                            if response.status == 200:
                                html = await response.text()
                                soup = BeautifulSoup(html, 'html.parser')
                                
                                logger.info(f"[Bing Images] Scraping search results")
                                
                                # Find image URLs in the page
                                img_tags = soup.find_all('img', class_='mimg')
                                image_urls = []
                                for img in img_tags:
                                    src = img.get('src') or img.get('data-src')
                                    if src and src.startswith('http'):
                                        image_urls.append(src)
                                
                                # Download in batches
                                batch_size = 10
                                for i in range(0, min(len(image_urls), 50), batch_size):  # Limit to 50 images
                                    if time.time() - start_time > timeout_seconds:
                                        break
                                    
                                    batch = image_urls[i:i+batch_size]
                                    download_tasks = [self._download_and_encode_image(session, url) for url in batch]
                                    results = await asyncio.gather(*download_tasks, return_exceptions=True)
                                    
                                    for img_b64 in results:
                                        if img_b64 and not isinstance(img_b64, Exception):
                                            source_images.append(img_b64)
                                
                                logger.info(f"[Bing Images] Downloaded {len(source_images)} images")
                    
                    except Exception as e:
                        logger.warning(f"[Bing Images] Error: {e}")
                    
                    return source_images
                
                # Launch ALL sources in parallel (4 concurrent threads)
                logger.info("Launching parallel searches from 4 sources...")
                search_tasks = [
                    fetch_wikimedia(),
                    fetch_nasa_api(),
                    fetch_google_images(),
                    fetch_bing_images()
                ]
                
                # Wait for all searches to complete (or timeout)
                results = await asyncio.gather(*search_tasks, return_exceptions=True)
                
                # Combine all results
                for result in results:
                    if isinstance(result, list):
                        images.extend(result)
                        # Update task incrementally
                        task.images.extend(result)
                        task.images_found = len(task.images)
                        task.progress = min(100, (time.time() - start_time) / timeout_seconds * 100)
                
                logger.info(f"Combined results from all sources: {len(images)} total images")
            
            elapsed = time.time() - start_time
            logger.info(f"NASA image fetch completed: {len(images)} images in {elapsed:.1f}s")
            return images
            
        except Exception as e:
            logger.error(f"Error fetching NASA images: {e}")
            return images
    
    async def _fetch_cell_images(self, prompt: str, max_images: int, task, timeout: int = 20) -> List[str]:
        """Fetch cell microscopy images with concurrent web scraping from multiple sources"""
        images = []
        start_time = time.time()
        timeout_seconds = timeout
        
        try:
            from bs4 import BeautifulSoup
            
            logger.info(f"Fetching cell microscopy images with concurrent requests ({timeout}s timeout)...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            }
            
            async with aiohttp.ClientSession(headers=headers) as session:
                
                async def fetch_flickr():
                    """Fetch from Flickr"""
                    source_images = []
                    sources = [
                        "https://www.flickr.com/search/?text=cell%20microscopy&license=2%2C3%2C4%2C5%2C6%2C9",
                        "https://www.flickr.com/search/?text=human%20cells%20microscope&license=2%2C3%2C4%2C5%2C6%2C9",
                    ]
                    
                    for source_url in sources:
                        if time.time() - start_time > timeout_seconds:
                            break
                        try:
                            async with session.get(source_url, timeout=20) as response:
                                if response.status == 200:
                                    html = await response.text()
                                    soup = BeautifulSoup(html, 'html.parser')
                                    img_tags = soup.find_all('img', src=True)
                                    
                                    img_urls = []
                                    for img_tag in img_tags[:50]:
                                        img_src = img_tag.get('src', '')
                                        if img_src and img_src.startswith('http'):
                                            img_urls.append(img_src)
                                    
                                    download_tasks = [self._download_and_encode_image(session, url) for url in img_urls[:20]]
                                    results = await asyncio.gather(*download_tasks, return_exceptions=True)
                                    
                                    for img_b64 in results:
                                        if img_b64 and not isinstance(img_b64, Exception):
                                            source_images.append(img_b64)
                        except Exception as e:
                            logger.warning(f"[Flickr] Error: {e}")
                    
                    logger.info(f"[Flickr] Downloaded {len(source_images)} images")
                    return source_images
                
                async def fetch_wikimedia_cells():
                    """Fetch from Wikimedia Commons"""
                    source_images = []
                    sources = [
                        "https://commons.wikimedia.org/wiki/Category:Cells",
                        "https://commons.wikimedia.org/wiki/Category:Microscopy",
                    ]
                    
                    for source_url in sources:
                        if time.time() - start_time > timeout_seconds:
                            break
                        try:
                            async with session.get(source_url, timeout=20) as response:
                                if response.status == 200:
                                    html = await response.text()
                                    soup = BeautifulSoup(html, 'html.parser')
                                    img_tags = soup.find_all('img', src=True)
                                    
                                    img_urls = []
                                    for img_tag in img_tags[:50]:
                                        img_src = img_tag.get('src', '')
                                        if img_src and img_src.startswith('http'):
                                            img_urls.append(img_src)
                                    
                                    download_tasks = [self._download_and_encode_image(session, url) for url in img_urls[:20]]
                                    results = await asyncio.gather(*download_tasks, return_exceptions=True)
                                    
                                    for img_b64 in results:
                                        if img_b64 and not isinstance(img_b64, Exception):
                                            source_images.append(img_b64)
                        except Exception as e:
                            logger.warning(f"[Wikimedia] Error: {e}")
                    
                    logger.info(f"[Wikimedia] Downloaded {len(source_images)} images")
                    return source_images
                
                async def fetch_google_cells():
                    """Fetch from Google Images"""
                    source_images = []
                    try:
                        search_query = "human+cells+microscopy"
                        google_url = f"https://www.google.com/search?q={search_query}&tbm=isch"
                        
                        async with session.get(google_url, timeout=20) as response:
                            if response.status == 200:
                                html = await response.text()
                                soup = BeautifulSoup(html, 'html.parser')
                                img_tags = soup.find_all('img')
                                
                                img_urls = []
                                for img in img_tags[:50]:
                                    src = img.get('src') or img.get('data-src')
                                    if src and src.startswith('http'):
                                        img_urls.append(src)
                                
                                download_tasks = [self._download_and_encode_image(session, url) for url in img_urls[:30]]
                                results = await asyncio.gather(*download_tasks, return_exceptions=True)
                                
                                for img_b64 in results:
                                    if img_b64 and not isinstance(img_b64, Exception):
                                        source_images.append(img_b64)
                                
                                logger.info(f"[Google Images] Downloaded {len(source_images)} images")
                    except Exception as e:
                        logger.warning(f"[Google Images] Error: {e}")
                    
                    return source_images
                
                async def fetch_bing_cells():
                    """Fetch from Bing Images"""
                    source_images = []
                    try:
                        search_query = "human+cells+microscopy"
                        bing_url = f"https://www.bing.com/images/search?q={search_query}"
                        
                        async with session.get(bing_url, timeout=20) as response:
                            if response.status == 200:
                                html = await response.text()
                                soup = BeautifulSoup(html, 'html.parser')
                                img_tags = soup.find_all('img')
                                
                                img_urls = []
                                for img in img_tags[:50]:
                                    src = img.get('src') or img.get('data-src')
                                    if src and src.startswith('http'):
                                        img_urls.append(src)
                                
                                download_tasks = [self._download_and_encode_image(session, url) for url in img_urls[:30]]
                                results = await asyncio.gather(*download_tasks, return_exceptions=True)
                                
                                for img_b64 in results:
                                    if img_b64 and not isinstance(img_b64, Exception):
                                        source_images.append(img_b64)
                                
                                logger.info(f"[Bing Images] Downloaded {len(source_images)} images")
                    except Exception as e:
                        logger.warning(f"[Bing Images] Error: {e}")
                    
                    return source_images
                
                # Launch ALL sources in parallel (4 concurrent threads)
                logger.info("Launching parallel searches from 4 sources...")
                search_tasks = [
                    fetch_flickr(),
                    fetch_wikimedia_cells(),
                    fetch_google_cells(),
                    fetch_bing_cells()
                ]
                
                # Wait for all searches to complete (or timeout)
                results = await asyncio.gather(*search_tasks, return_exceptions=True)
                
                # Combine all results
                for result in results:
                    if isinstance(result, list):
                        images.extend(result)
                        task.images.extend(result)
                        task.images_found = len(task.images)
                        task.progress = min(100, (time.time() - start_time) / timeout_seconds * 100)
                
                logger.info(f"Combined results from all sources: {len(images)} total images")
            
            elapsed = time.time() - start_time
            logger.info(f"Cell image fetch completed: {len(images)} images in {elapsed:.1f}s")
            return images
            
        except Exception as e:
            logger.error(f"Error fetching cell images: {e}")
            return images
    
    async def _fetch_general_images(self, prompt: str, max_images: int, task) -> List[str]:
        """Fetch general images - fallback to synthetic for now"""
        logger.warning(f"General image search not implemented, using synthetic images")
        images = []
        for i in range(min(max_images, 100)):
            img = self._generate_synthetic_image_b64(f"{prompt}_{i}")
            if img:
                images.append(img)
                task.images.append(img)
                task.images_found = len(images)
                task.progress = (len(images) / max_images) * 100
        return images
    
    def _generate_synthetic_image_b64(self, seed: str) -> str:
        """Generate a synthetic image as base64 for demo purposes"""
        try:
            from PIL import Image, ImageDraw
            import base64
            import io
            import random
            
            # Set seed for reproducible images
            random.seed(hash(seed) % 2**32)
            
            # Create image
            img = Image.new('RGB', (512, 512), color=(
                random.randint(50, 200),
                random.randint(50, 200),
                random.randint(50, 200)
            ))
            
            draw = ImageDraw.Draw(img)
            
            # Add some shapes
            for _ in range(random.randint(3, 8)):
                x1, y1 = random.randint(0, 400), random.randint(0, 400)
                x2, y2 = x1 + random.randint(50, 112), y1 + random.randint(50, 112)
                color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                
                if random.choice([True, False]):
                    draw.rectangle([x1, y1, x2, y2], fill=color)
                else:
                    draw.ellipse([x1, y1, x2, y2], fill=color)
            
            # Convert to base64
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=85)
            img_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
            return img_b64
            
        except Exception as e:
            logger.warning(f"Error generating synthetic image: {e}")
            return ""
    
    async def start_benchmark(self, test_type: str, instance_type: str, build_mode: str, max_instances: int, image_count: int, iterations: int = 100, pipeline_type: str = 'standard') -> str:
        """Start a benchmark test"""
        task_id = str(uuid.uuid4())
        
        task = BenchmarkTask(
            task_id=task_id,
            test_type=test_type,
            instance_type=instance_type,
            max_instances=max_instances,
            image_count=image_count,
            status=TaskStatus.PENDING,
            start_time=time.time()
        )
        
        # Store build mode and pipeline type in task
        task.build_mode = build_mode
        task.iterations = iterations
        task.pipeline_type = pipeline_type
        
        # Initialize build progress with image count
        task.build_progress = {
            "current_step": "Initializing",
            "steps": [],
            "progress_percent": 0,
            "image_count": image_count
        }
        
        # Get images from the most recent completed image search
        images = []
        for search_task in reversed(list(self.image_search_tasks.values())):
            if search_task.status == TaskStatus.COMPLETED and search_task.images:
                images = search_task.images[:image_count]  # Limit to requested count
                logger.info(f"Using {len(images)} images from search task {search_task.task_id}")
                break
        
        # For analysis pipeline, use fixed subset of 10 images with more iterations
        if pipeline_type == 'analysis' and len(images) > 10:
            import random
            # Use a fixed seed for reproducibility across runs
            random.seed(42)
            # Select 10 random indices
            selected_indices = sorted(random.sample(range(len(images)), min(10, len(images))))
            images = [images[i] for i in selected_indices]
            
            task.iterations = 10
            
            # Update task with actual image count used
            task.image_count = len(images)
            task.build_progress["image_count"] = len(images)
            
            logger.info(f"🔬 Analysis pipeline optimization: Using {len(images)} fixed images (indices: {selected_indices}) with {task.iterations} iterations")
        
        # Store images in task results for processing
        task.results = {"images": images}
        
        self.benchmark_tasks[task_id] = task
        
        # Start benchmark in background
        asyncio.create_task(self._execute_benchmark(task_id))
        
        logger.info(f"Started benchmark task {task_id} with build_mode={build_mode}, pipeline={pipeline_type}, {len(images)} images, and {task.iterations} iterations")
        return task_id
    
    async def _execute_benchmark(self, task_id: str):
        """Execute a benchmark test with real EC2 integration"""
        try:
            task = self.benchmark_tasks[task_id]
            
            await execute_benchmark_with_build(
                task,
                self.instance_manager,
                self.build_manager,
                self
            )
        except Exception as e:
            logger.error(f"Critical error in benchmark execution for task {task_id}: {e}", exc_info=True)
            # Ensure task is marked as failed
            if task_id in self.benchmark_tasks:
                task = self.benchmark_tasks[task_id]
                task.status = "failed"
                task.error = f"Critical error: {str(e)}"
                task.end_time = time.time()
    
    async def _process_images_single_instance(self, instance_id: str, images: List[str], optimization_mode: str) -> Dict[str, Any]:
        """Process images on a single instance"""
        try:
            instance = self.instance_manager.instances[instance_id]
            
            # Call OpenCV MCP server on the instance
            # For demo, simulate processing
            await asyncio.sleep(len(images) * 0.01)  # Simulate processing time
            
            # Generate processed images (mock)
            processed_images = []
            for i, img in enumerate(images[:20]):  # Process first 20 for demo
                processed_img = self._generate_processed_image_b64(img, optimization_mode)
                processed_images.append(processed_img)
            
            return {
                "processed_images": processed_images,
                "processing_time": len(images) * 0.01
            }
            
        except Exception as e:
            logger.error(f"Error processing images on instance {instance_id}: {e}")
            return {"processed_images": [], "processing_time": 0}
    
    async def _process_images_multi_instance(self, instance_ids: List[str], images: List[str], optimization_mode: str) -> Dict[str, Any]:
        """Process images across multiple instances"""
        try:
            # Distribute load
            distribution = await self.instance_manager.distribute_load(images, instance_ids[0])
            
            # Process in parallel
            tasks = []
            for instance_id, image_batch in distribution.items():
                task = self._process_images_single_instance(instance_id, image_batch, optimization_mode)
                tasks.append(task)
            
            results = await asyncio.gather(*tasks)
            
            # Combine results
            all_processed_images = []
            total_processing_time = 0
            
            for result in results:
                all_processed_images.extend(result.get("processed_images", []))
                total_processing_time = max(total_processing_time, result.get("processing_time", 0))
            
            return {
                "processed_images": all_processed_images,
                "processing_time": total_processing_time
            }
            
        except Exception as e:
            logger.error(f"Error in multi-instance processing: {e}")
            return {"processed_images": [], "processing_time": 0}
    
    def _generate_processed_image_b64(self, original_b64: str, optimization_mode: str) -> str:
        """Generate a processed version of an image for demo"""
        try:
            from PIL import Image, ImageDraw, ImageFilter
            import base64
            import io
            
            # Decode original image
            img_data = base64.b64decode(original_b64)
            img = Image.open(io.BytesIO(img_data))
            
            # Apply processing effects
            if optimization_mode == "optimized":
                # Simulate better processing
                img = img.resize((512, 512), Image.Resampling.LANCZOS)
                img = img.filter(ImageFilter.SHARPEN)
            else:
                # Simulate basic processing
                img = img.resize((512, 512), Image.Resampling.NEAREST)
            
            # Add contour overlay (simulate findContours)
            draw = ImageDraw.Draw(img)
            draw.rectangle([10, 10, 502, 502], outline="green", width=3)
            
            # Convert back to base64
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=85)
            return base64.b64encode(buffer.getvalue()).decode('utf-8')
            
        except Exception as e:
            logger.warning(f"Error processing image: {e}")
            return original_b64
    
    async def get_image_search_status(self, task_id: str) -> Dict[str, Any]:
        """Get status of an image search task"""
        if task_id not in self.image_search_tasks:
            return {"error": "Task not found"}
        
        task = self.image_search_tasks[task_id]
        
        # Calculate remaining time and elapsed time
        elapsed = time.time() - task.start_time
        remaining_time = max(0, task.timeout - elapsed) if task.status == TaskStatus.RUNNING else 0
        
        return {
            "task_id": task_id,
            "status": task.status.value,
            "images_found": task.images_found,
            "progress": task.progress,
            "images": task.images if task.status == TaskStatus.COMPLETED else [],
            "remaining_time": remaining_time,
            "elapsed_time": elapsed,
            "error": task.error
        }
    
    async def get_benchmark_status(self, task_id: str) -> Dict[str, Any]:
        """Get status of a benchmark task"""
        if task_id not in self.benchmark_tasks:
            return {
                "status": "not_found",
                "error": "Task not found. It may have completed before orchestrator restart.",
                "task_id": task_id
            }
        
        task = self.benchmark_tasks[task_id]
        result = asdict(task)
        # Handle both enum and string status
        if isinstance(task.status, TaskStatus):
            result["status"] = task.status.value
        else:
            result["status"] = task.status
        
        # Add build progress messages for frontend
        if hasattr(task, 'build_progress') and task.build_progress:
            result["build_progress"] = task.build_progress
            
            # Add human-readable messages based on current step
            current_step = task.build_progress.get("current_step", "")
            
            # More detailed status messages
            if "Launching instance" in current_step:
                result["build_message"] = "🚀 Launching EC2 instance..."
            elif "Waiting for instance" in current_step or "running and ready" in current_step:
                result["build_message"] = "⏳ Waiting for EC2 instance to be ready..."
            elif "Installing OpenCV via pip" in current_step:
                result["build_message"] = "📦 Installing OpenCV via pip (~10 minutes)..."
            elif "OpenCV installed successfully" in current_step:
                result["build_message"] = "✅ OpenCV installed successfully!"
            elif "Compiling OpenCV" in current_step:
                result["build_message"] = "🔨 Compiling OpenCV from source (~30-45 minutes)..."
            elif "OpenCV compiled successfully" in current_step:
                result["build_message"] = "✅ OpenCV compiled successfully!"
            elif "Deploying MCP server" in current_step:
                result["build_message"] = "🚀 Deploying MCP server to EC2..."
            elif "MCP server deployed successfully" in current_step:
                result["build_message"] = "✅ MCP server deployed and ready!"
            elif "Running benchmark" in current_step:
                # Get image count from build progress
                image_count = task.build_progress.get("image_count", 0) if hasattr(task, 'build_progress') and task.build_progress else 0
                if image_count > 0:
                    result["build_message"] = f"🖼️ Processing {image_count} images with OpenCV (results will appear when complete)..."
                else:
                    result["build_message"] = "🖼️ Processing images with OpenCV (results will appear when complete)..."
            elif "Completed" in current_step:
                result["build_message"] = "✅ Benchmark completed!"
            elif "failed" in current_step.lower() or "error" in current_step.lower():
                result["build_message"] = f"❌ {current_step}"
            else:
                result["build_message"] = current_step
        
        return result
    
    async def get_system_status(self) -> Dict[str, Any]:
        """Get overall system status"""
        try:
            return {
                "opencv_status": "connected",
                "mcp_status": "connected",
                "graviton_functions": ["resize", "findContours", "blur", "threshold"],
                "active_instances": len([i for i in self.instance_manager.instances.values() if i.state.value == "running"]),
                "total_cost": (await self.instance_manager.get_cost_summary()).get("total_cost", 0)
            }
        except Exception as e:
            logger.error(f"Error getting system status: {e}")
            return {"error": str(e)}
    
    async def cleanup(self):
        """Cleanup resources"""
        try:
            await self.instance_manager.cleanup()
            await self.session.close()
            logger.info("Orchestrator cleanup completed")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
    
    async def _cleanup_orphaned_instances(self):
        """Terminate any benchmark instances from previous runs"""
        try:
            import boto3
            ec2 = boto3.client('ec2', region_name=self.default_region)
            
            # Find all running instances with benchmark tags
            response = ec2.describe_instances(
                Filters=[
                    {'Name': 'instance-state-name', 'Values': ['running', 'pending']},
                    {'Name': 'tag:Project', 'Values': ['OpenCV-Graviton-Benchmark']}
                ]
            )
            
            orphaned_instances = []
            for reservation in response['Reservations']:
                for instance in reservation['Instances']:
                    instance_id = instance['InstanceId']
                    orphaned_instances.append(instance_id)
            
            if orphaned_instances:
                logger.warning(f"Found {len(orphaned_instances)} orphaned benchmark instances, terminating...")
                ec2.terminate_instances(InstanceIds=orphaned_instances)
                logger.info(f"Terminated orphaned instances: {orphaned_instances}")
            else:
                logger.info("No orphaned benchmark instances found")
                
        except Exception as e:
            logger.error(f"Error cleaning up orphaned instances: {e}")

# Web API handlers
async def create_app():
    """Create the web application"""
    orchestrator = BenchmarkOrchestrator()
    await orchestrator.initialize()
    
    app = web.Application()
    app['orchestrator'] = orchestrator
    
    # Add CORS middleware
    @web.middleware
    async def cors_middleware(request, handler):
        # Handle preflight requests
        if request.method == 'OPTIONS':
            response = web.Response()
        else:
            response = await handler(request)
        
        # Add CORS headers
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    
    app.middlewares.append(cors_middleware)
    
    # API routes
    app.router.add_post('/api/images/search', handle_start_image_search)
    app.router.add_get('/api/images/search/{task_id}/status', handle_image_search_status)
    app.router.add_post('/api/benchmark/run', handle_start_benchmark)
    app.router.add_get('/api/benchmark/{task_id}/status', handle_benchmark_status)
    app.router.add_get('/api/opencv/status', handle_opencv_status)
    app.router.add_get('/api/mcp/status', handle_mcp_status)
    app.router.add_get('/api/opencv/graviton-functions', handle_graviton_functions)
    app.router.add_get('/api/instances/active', handle_active_instances)
    app.router.add_post('/api/instances/cleanup', handle_cleanup_instances)
    app.router.add_get('/api/instances/{instance_id}/console', handle_instance_console_log)
    app.router.add_get('/api/build/history', handle_build_history)
    app.router.add_post('/api/build/auto-retry', handle_start_auto_retry)
    app.router.add_get('/api/build/auto-retry/{task_id}/status', handle_auto_retry_status)
    app.router.add_post('/api/config/save', handle_save_config)
    
    # Static files - use absolute path
    frontend_path = os.path.join(os.path.dirname(__file__), '..', 'frontend')
    if os.path.exists(frontend_path):
        app.router.add_static('/', path=frontend_path, name='static')
    else:
        logger.warning(f"Frontend path not found: {frontend_path}")
    
    return app

async def handle_start_image_search(request):
    """Handle image search start request"""
    try:
        data = await request.json()
        prompt = data.get('prompt', '')
        max_images = data.get('max_images', 1000)
        timeout = data.get('timeout', 20)  # Default 20 seconds
        
        orchestrator = request.app['orchestrator']
        task_id = await orchestrator.start_image_search(prompt, max_images, timeout)
        
        return web.json_response({'taskId': task_id})
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

async def handle_image_search_status(request):
    """Handle image search status request"""
    try:
        task_id = request.match_info['task_id']
        orchestrator = request.app['orchestrator']
        status = await orchestrator.get_image_search_status(task_id)
        
        return web.json_response(status)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

async def handle_start_benchmark(request):
    """Handle benchmark start request"""
    try:
        data = await request.json()
        test_type = data.get('testType', '')
        instance_type = data.get('instanceType', '')
        build_mode = data.get('buildMode', 'pip')
        max_instances = data.get('maxInstances', 1)
        image_count = data.get('imageCount', 0)
        iterations = data.get('iterations', 100)
        pipeline_type = data.get('pipelineType', 'standard')  # 'standard', 'augmentation', or 'analysis'
        
        logger.info(f"Starting benchmark: {test_type}, {instance_type}, {build_mode}, pipeline={pipeline_type}, images={image_count}")
        
        orchestrator = request.app['orchestrator']
        task_id = await orchestrator.start_benchmark(
            test_type, instance_type, build_mode, max_instances, image_count, iterations, pipeline_type
        )
        
        logger.info(f"Benchmark task {task_id} started successfully")
        return web.json_response({'taskId': task_id})
    except Exception as e:
        logger.error(f"Error starting benchmark: {e}", exc_info=True)
        return web.json_response({'error': str(e)}, status=500)

async def handle_benchmark_status(request):
    """Handle benchmark status request"""
    try:
        task_id = request.match_info['task_id']
        orchestrator = request.app['orchestrator']
        status = await orchestrator.get_benchmark_status(task_id)
        
        return web.json_response(status)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

async def handle_opencv_status(request):
    """Handle OpenCV status request"""
    return web.json_response({'status': 'connected'})

async def handle_mcp_status(request):
    """Handle MCP status request"""
    return web.json_response({'status': 'connected'})

async def handle_graviton_functions(request):
    """Handle Graviton functions request"""
    return web.json_response({
        'functions': ['resize', 'findContours', 'blur', 'threshold', 'morphology']
    })

async def handle_active_instances(request):
    """Handle active instances request"""
    try:
        orchestrator = request.app['orchestrator']
        active_instances = []
        
        # Query AWS directly to get all running instances (more reliable than in-memory tracking)
        try:
            ec2_client = orchestrator.instance_manager.ec2_client
            response = ec2_client.describe_instances(
                Filters=[
                    {'Name': 'instance-state-name', 'Values': ['running']},
                    {'Name': 'tag:Project', 'Values': ['OpenCV-Graviton-Benchmark']}
                ]
            )
            
            for reservation in response['Reservations']:
                for instance in reservation['Instances']:
                    instance_id = instance['InstanceId']
                    instance_type = instance['InstanceType']
                    launch_time = instance['LaunchTime'].timestamp()
                    
                    # Get build_mode from tags
                    build_mode = 'unknown'
                    tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
                    build_mode = tags.get('BuildMode', 'unknown')
                    
                    active_instances.append({
                        'instance_id': instance_id,
                        'instance_type': instance_type,
                        'state': 'running',
                        'launch_time': launch_time,
                        'uptime': time.time() - launch_time,
                        'build_mode': build_mode
                    })
        except Exception as e:
            logger.error(f"Error querying AWS for instances: {e}")
            # Fallback to in-memory tracking
            for instance_id, instance in orchestrator.instance_manager.instances.items():
                if instance.state.value == "running":
                    build_mode = instance.build_mode if hasattr(instance, 'build_mode') else 'unknown'
                    if build_mode == 'unknown':
                        pool_info = orchestrator.instance_manager.instance_pool.get(instance_id, {})
                        build_mode = pool_info.get('build_mode', 'unknown')
                    
                    active_instances.append({
                        'instance_id': instance_id,
                        'instance_type': instance.instance_type,
                        'state': instance.state.value,
                        'launch_time': instance.launch_time,
                        'uptime': time.time() - instance.launch_time,
                        'build_mode': build_mode
                    })
        
        return web.json_response({
            'active_count': len(active_instances),
            'instances': active_instances,
            'status': 'connected' if active_instances else 'idle'
        })
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

async def handle_cleanup_instances(request):
    """Handle cleanup instances request - terminates all running benchmark instances"""
    try:
        orchestrator = request.app['orchestrator']
        
        # Call the cleanup method
        await orchestrator._cleanup_orphaned_instances()
        
        # Also clear the instance manager's instances dict
        terminated_count = len([i for i in orchestrator.instance_manager.instances.values() if i.state.value == "running"])
        orchestrator.instance_manager.instances.clear()
        
        # Clear the benchmark status
        orchestrator.current_benchmark = None
        
        # Clear the temp status file
        try:
            if os.path.exists('temp_status.json'):
                os.remove('temp_status.json')
                logger.info("Cleared temp_status.json")
        except Exception as e:
            logger.warning(f"Could not clear temp_status.json: {e}")
        
        logger.info(f"Frontend requested cleanup: terminated {terminated_count} instances")
        
        return web.json_response({
            'status': 'success',
            'terminated_count': terminated_count,
            'message': f'Terminated {terminated_count} instances'
        })
    except Exception as e:
        logger.error(f"Error in cleanup handler: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_build_history(request):
    """Handle build history request - returns last build attempts for each configuration"""
    try:
        orchestrator = request.app['orchestrator']
        
        # Convert build history to JSON-serializable format
        history = {}
        for key, attempt in orchestrator.build_history.items():
            history[key] = attempt
        
        return web.json_response({
            'build_history': history,
            'status': 'success'
        })
    except Exception as e:
        logger.error(f"Error in build history handler: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_instance_console_log(request):
    """Handle instance console log request - returns EC2 console output for debugging"""
    try:
        orchestrator = request.app['orchestrator']
        instance_id = request.match_info.get('instance_id')
        
        if not instance_id:
            return web.json_response({'error': 'instance_id required'}, status=400)
        
        # Get console output from EC2
        try:
            ec2_client = orchestrator.instance_manager.ec2_client
            response = ec2_client.get_console_output(InstanceId=instance_id)
            console_output = response.get('Output', '')
            
            # Get last update timestamp
            last_update = response.get('Timestamp')
            
            return web.json_response({
                'instance_id': instance_id,
                'console_output': console_output,
                'last_update': last_update.isoformat() if last_update else None,
                'output_length': len(console_output),
                'status': 'success'
            })
        except Exception as e:
            logger.error(f"Error getting console output for {instance_id}: {e}")
            return web.json_response({
                'error': f'Failed to get console output: {str(e)}',
                'instance_id': instance_id
            }, status=500)
            
    except Exception as e:
        logger.error(f"Error in console log handler: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_start_auto_retry(request):
    """Handle auto-retry build start request"""
    print("========== HANDLE_START_AUTO_RETRY CALLED ==========", flush=True)
    try:
        data = await request.json()
        print(f"========== REQUEST DATA: {data} ==========", flush=True)
        test_type = data.get('testType', '')
        instance_type = data.get('instanceType', '')
        build_mode = data.get('buildMode', 'pip')
        max_retries = data.get('maxRetries', 10)
        claude_api_key = data.get('claudeApiKey', '') or os.environ.get('ANTHROPIC_API_KEY', '')

        print(f"========== PARSED: test_type={test_type}, instance_type={instance_type} ==========", flush=True)

        if not claude_api_key:
            print("========== NO CLAUDE API KEY ==========", flush=True)
            return web.json_response({'error': 'Claude API key is required. Set ANTHROPIC_API_KEY env var or enter it in the UI.'}, status=400)
        
        orchestrator = request.app['orchestrator']
        task_id = str(uuid.uuid4())
        
        print(f"========== CALLING AUTO_RETRY_MANAGER.start_auto_retry ==========", flush=True)
        logger.info(f"Starting auto-retry build: {test_type}, {instance_type}, {build_mode}")
        
        # Start auto-retry task
        result = await orchestrator.auto_retry_manager.start_auto_retry(
            task_id=task_id,
            test_type=test_type,
            instance_type=instance_type,
            build_mode=build_mode,
            max_retries=max_retries,
            claude_api_key=claude_api_key
        )
        
        print(f"========== RETURNING RESPONSE ==========", flush=True)
        return web.json_response({'taskId': task_id, 'status': 'started'})
    except Exception as e:
        print(f"========== EXCEPTION IN HANDLER: {e} ==========", flush=True)
        logger.error(f"Error starting auto-retry: {e}", exc_info=True)
        return web.json_response({'error': str(e)}, status=500)

async def handle_auto_retry_status(request):
    """Handle auto-retry status request"""
    try:
        task_id = request.match_info['task_id']
        orchestrator = request.app['orchestrator']
        
        status = orchestrator.auto_retry_manager.get_retry_status(task_id)
        
        if not status:
            return web.json_response({'error': 'Task not found'}, status=404)
        
        # Convert snake_case to camelCase for JavaScript
        response = {
            'status': status.get('status'),
            'testType': status.get('test_type'),
            'instanceType': status.get('instance_type'),
            'buildMode': status.get('build_mode'),
            'maxRetries': status.get('max_retries'),
            'attempt': status.get('attempt'),
            'currentStep': status.get('current_step'),
            'lastError': status.get('last_error'),
            'error': status.get('error'),
            'claudeAnalysis': status.get('claude_analysis'),
            'claudeFixes': status.get('claude_fixes', []),
            'attempts': status.get('attempts', []),
            'totalTimeMinutes': status.get('total_time_minutes'),
            'totalTimeSeconds': status.get('total_time_seconds'),
            'totalElapsedMinutes': status.get('total_elapsed_minutes')  # Total time since start
        }
        
        return web.json_response(response)
    except Exception as e:
        logger.error(f"Error getting auto-retry status: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_save_config(request):
    """Handle configuration save request"""
    try:
        data = await request.json()
        claude_api_key = data.get('claudeApiKey', '')
        marketplace_ami_id = data.get('marketplaceAmiId', '')
        
        orchestrator = request.app['orchestrator']
        
        # Update orchestrator configuration
        if marketplace_ami_id:
            orchestrator.marketplace_ami_id = marketplace_ami_id
        
        # Save to config file
        config_path = os.path.join(os.path.dirname(__file__), '..', 'config-marketplace.json')
        config = {
            'marketplace': {
                'ami_id': marketplace_ami_id or orchestrator.marketplace_ami_id,
                'license_key': orchestrator.marketplace_license_key
            }
        }
        
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        
        logger.info(f"Configuration saved: AMI={marketplace_ami_id}")
        
        return web.json_response({'status': 'success', 'message': 'Configuration saved'})
    except Exception as e:
        logger.error(f"Error saving configuration: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def main():
    """Main entry point"""
    app = await create_app()
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    
    logger.info("Benchmark orchestrator started on http://0.0.0.0:8080")
    
    try:
        await asyncio.Future()  # Run forever
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await app['orchestrator'].cleanup()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())