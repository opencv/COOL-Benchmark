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
    opencv_version: str = "4"
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
    
    async def start_local_image_load(self, directory: str, max_images: int = 1000) -> str:
        """Load images from a local directory into an ImageSearchTask"""
        task_id = str(uuid.uuid4())

        task = ImageSearchTask(
            task_id=task_id,
            prompt=f"local:{directory}",
            status=TaskStatus.PENDING,
            images=[],
            start_time=time.time(),
            timeout=0
        )

        self.image_search_tasks[task_id] = task
        asyncio.create_task(self._execute_local_image_load(task_id, directory, max_images))

        logger.info(f"Started local image load task {task_id} from {directory}")
        return task_id

    async def _execute_local_image_load(self, task_id: str, directory: str, max_images: int):
        """Execute local image loading from disk"""
        try:
            task = self.image_search_tasks[task_id]
            task.status = TaskStatus.RUNNING

            if not os.path.isdir(directory):
                raise ValueError(f"Directory not found: {directory}")

            logger.info(f"📁 Loading local images from: {directory}")
            await self._load_local_images(task, directory, max_images)

            task.status = TaskStatus.COMPLETED
            task.progress = 100.0
            logger.info(f"✅ Local image load task {task_id} completed with {task.images_found} images")

        except Exception as e:
            logger.error(f"Error in local image load task {task_id}: {e}")
            task = self.image_search_tasks.get(task_id)
            if task:
                task.status = TaskStatus.FAILED
                task.error = str(e)

    async def _load_local_images(self, task, directory: str, max_images: int) -> List[str]:
        """Load images from a local directory and encode as base64"""
        from PIL import Image
        import base64
        import io

        supported_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}
        try:
            image_files = sorted([
                f for f in os.listdir(directory)
                if os.path.splitext(f.lower())[1] in supported_exts
            ])
        except Exception as e:
            logger.error(f"Cannot list directory {directory}: {e}")
            return []

        if max_images:
            image_files = image_files[:max_images]

        total = len(image_files)
        logger.info(f"📁 Found {total} images in {directory}")

        for filename in image_files:
            path = os.path.join(directory, filename)
            try:
                with open(path, 'rb') as f:
                    content = f.read()

                img = Image.open(io.BytesIO(content))
                img.thumbnail((512, 512), Image.Resampling.LANCZOS)

                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')

                output = io.BytesIO()
                img.save(output, format='JPEG', quality=85)
                encoded = base64.b64encode(output.getvalue()).decode('utf-8')

                task.images.append(encoded)
                task.images_found = len(task.images)
                task.progress = (task.images_found / total) * 100 if total > 0 else 100.0

            except Exception as e:
                logger.warning(f"Skipping local image {filename}: {e}")

        return task.images

    
    async def start_benchmark(self, test_type: str, instance_type: str, build_mode: str, max_instances: int, image_count: int, iterations: int = 100, pipeline_type: str = 'standard', opencv_version: str = '4') -> str:
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
        
        # Store build mode, pipeline type, and OpenCV version in task
        task.build_mode = build_mode
        task.iterations = iterations
        task.pipeline_type = pipeline_type
        task.opencv_version = opencv_version
        
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
    app.router.add_get('/api/images/search/{task_id}/status', handle_image_search_status)
    app.router.add_post('/api/images/local', handle_load_local_images)
    app.router.add_post('/api/benchmark/run', handle_start_benchmark)
    app.router.add_get('/api/benchmark/{task_id}/status', handle_benchmark_status)
    app.router.add_get('/api/opencv/status', handle_opencv_status)
    app.router.add_get('/api/mcp/status', handle_mcp_status)
    app.router.add_get('/api/opencv/graviton-functions', handle_graviton_functions)
    app.router.add_get('/api/instances/active', handle_active_instances)
    app.router.add_post('/api/instances/cleanup', handle_cleanup_instances)
    app.router.add_get('/api/instances/{instance_id}/console', handle_instance_console_log)
    app.router.add_get('/api/build/history', handle_build_history)
    app.router.add_post('/api/config/save', handle_save_config)
    
    # Static files - use absolute path
    frontend_path = os.path.join(os.path.dirname(__file__), '..', 'frontend')
    if os.path.exists(frontend_path):
        app.router.add_static('/', path=frontend_path, name='static')
    else:
        logger.warning(f"Frontend path not found: {frontend_path}")
    
    return app

async def handle_image_search_status(request):
    """Handle image search status request"""
    try:
        task_id = request.match_info['task_id']
        orchestrator = request.app['orchestrator']
        status = await orchestrator.get_image_search_status(task_id)
        
        return web.json_response(status)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

async def handle_load_local_images(request):
    """Handle local image load request — reads images from the local assets directory"""
    try:
        data = await request.json()
        default_assets = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'assets'))
        directory = data.get('directory', default_assets)
        max_images = data.get('max_images', 1000)

        orchestrator = request.app['orchestrator']
        task_id = await orchestrator.start_local_image_load(directory, max_images)

        return web.json_response({'taskId': task_id})
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
        opencv_version = data.get('opencvVersion', '4')  # '4' or '5'

        logger.info(f"Starting benchmark: {test_type}, {instance_type}, {build_mode}, pipeline={pipeline_type}, opencv={opencv_version}, images={image_count}")

        orchestrator = request.app['orchestrator']
        task_id = await orchestrator.start_benchmark(
            test_type, instance_type, build_mode, max_instances, image_count, iterations, pipeline_type, opencv_version
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

async def handle_save_config(request):
    """Handle configuration save request"""
    try:
        data = await request.json()
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