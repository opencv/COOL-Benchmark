#!/usr/bin/env python3
"""
OpenCV MCP Server - Runs on EC2 instances to process images
Receives images via HTTP POST and returns processed results
"""

import asyncio
import json
import logging
import time
import base64
import sys
import traceback
from io import BytesIO
from aiohttp import web

# Configure logging to both file and stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/var/log/opencv-mcp.log')
    ]
)
logger = logging.getLogger("opencv-mcp-server")

# OpenCV imports
try:
    import cv2
    import numpy as np
    from PIL import Image
    OPENCV_AVAILABLE = True
    logger.info(f"✅ OpenCV {cv2.__version__} loaded successfully")
    logger.info(f"📍 OpenCV location: {cv2.__file__}")
    
    build_info = cv2.getBuildInformation()
    if 'NEON' in build_info or 'neon' in build_info:
        logger.info("🚀 NEON SIMD optimizations: ENABLED")
    else:
        logger.info("⚠️  NEON SIMD optimizations: NOT DETECTED")
    
    if 'AVX' in build_info or 'SSE' in build_info:
        logger.info("🚀 x86 SIMD optimizations (AVX/SSE): ENABLED")
    
    if '/opt/cool' in cv2.__file__:
        logger.info("🎯 Using COOL optimized OpenCV build")
    else:
        logger.info("📦 Using standard OpenCV build")
    
    MEMORY_BENCHMARK_RESULTS = {}
    try:
        for size_bytes, label in [(100*1024*1024, "100MB")]:
            arr = np.zeros(size_bytes, dtype=np.uint8)
            t = time.time(); arr[:] = 255; w_bw = size_bytes/(1024**3)/(time.time()-t)
            t = time.time(); _ = arr.sum(); r_bw = size_bytes/(1024**3)/(time.time()-t)
            idx = np.random.randint(0, size_bytes, 5000)
            t = time.time()
            for i in idx: _ = arr[i]
            lat = (time.time()-t)/len(idx)*1000000
            MEMORY_BENCHMARK_RESULTS[label] = {'write_gbps': round(w_bw,1), 'read_gbps': round(r_bw,1), 'random_latency_us': round(lat,2)}
            logger.info(f"💾 Mem: W:{w_bw:.1f}GB/s R:{r_bw:.1f}GB/s Lat:{lat:.2f}µs")
            del arr
    except: pass
        
except ImportError as e:
    OPENCV_AVAILABLE = False
    logger.error(f"OpenCV not available: {e}")

def _get_cache_info():
    cache_info = {}
    try:
        import subprocess
        r = subprocess.run(['lscpu'], capture_output=True, text=True, timeout=2)
        for line in r.stdout.split('\n'):
            if 'L1d' in line: cache_info['L1'] = {'size': line.split(':')[1].strip()}
            elif 'L2' in line: cache_info['L2'] = {'size': line.split(':')[1].strip()}
            elif 'L3' in line: cache_info['L3'] = {'size': line.split(':')[1].strip()}
    except: pass
    return cache_info

async def handle_process(request):
    """Process images with OpenCV, branching by pipeline_type.
    Reports ONLY OpenCV function time (no base64 decode/encode overhead)."""
    try:
        logger.info("Received process request")

        if not OPENCV_AVAILABLE:
            logger.error("OpenCV not available")
            return web.json_response({
                'error': 'OpenCV not installed on this instance'
            }, status=500)

        # Request parsing is NOT counted in processing_time
        try:
            data = await asyncio.wait_for(request.json(), timeout=60)
            logger.info("Request JSON parsed successfully")
        except asyncio.TimeoutError:
            logger.error("Timeout reading request JSON")
            return web.json_response({'error': 'Request timeout'}, status=408)

        images_b64 = data.get('images', [])
        iterations = data.get('iterations', 1)
        build_mode = data.get('build_mode', 'pip')
        pipeline_type = data.get('pipeline_type', 'standard')

        if not images_b64:
            logger.error("No images provided in request")
            return web.json_response({'error': 'No images provided'}, status=400)

        logger.info(f"Processing {len(images_b64)} images with {iterations} iterations "
                     f"(build_mode: {build_mode}, pipeline: {pipeline_type})")

        processed_images = []
        images_processed_count = 0

        # This is the ONLY time that will go into response["processing_time"]
        opencv_total_time = 0.0
        wall_total_time = 0.0

        # Define operation_times based on pipeline type
        if pipeline_type == 'augmentation':
            operation_times = {
                'rotate': 0.0, 'resize_2x': 0.0, 'medianBlur': 0.0,
                'float_convert': 0.0, 'GaussianBlur': 0.0,
            }
        elif pipeline_type == 'analysis':
            operation_times = {
                'cvtColor': 0.0, 'CLAHE': 0.0, 'calcHist': 0.0,
                'HoughCircles': 0.0, 'findContours': 0.0,
            }
        else:
            # standard pipeline
            operation_times = {
                'resize': 0.0, 'cvtColor': 0.0, 'blur': 0.0,
                'threshold': 0.0, 'findContours': 0.0,
            }

        for iteration in range(iterations):
            iteration_wall_start = time.perf_counter()
            iteration_opencv_time = 0.0
            logger.info(f"🔄 Starting iteration {iteration+1}/{iterations} [{pipeline_type} pipeline]")

            for idx, img_b64 in enumerate(images_b64):
                try:
                    logger.info(f"📸 [Iter {iteration+1}/{iterations}] [Img {idx+1}/{len(images_b64)}] Processing ({pipeline_type})")

                    # NOT counted: base64 decode + numpy buffer creation
                    img_data = base64.b64decode(img_b64)
                    img_array = np.frombuffer(img_data, dtype=np.uint8)

                    # imdecode (not counted in opencv_total_time)
                    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                    if img is None:
                        logger.warning(f"Failed to decode image {idx}")
                        continue

                    # ========================================
                    # PIPELINE-SPECIFIC OPERATIONS
                    # ========================================

                    if pipeline_type == 'augmentation':
                        # --- AUGMENTATION PIPELINE ---
                        # 1. Rotate 90° clockwise
                        t0 = time.perf_counter()
                        img_proc = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] rotate: {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['rotate'] += dt

                        # 2. Resize 2x (upscale)
                        t0 = time.perf_counter()
                        h, w = img_proc.shape[:2]
                        img_proc = cv2.resize(img_proc, (w * 2, h * 2), interpolation=cv2.INTER_LINEAR)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] resize_2x: {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['resize_2x'] += dt

                        # 3. Median blur (5x5)
                        t0 = time.perf_counter()
                        img_proc = cv2.medianBlur(img_proc, 5)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] medianBlur: {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['medianBlur'] += dt

                        # 4. Convert to float32 and back (simulate float pipeline)
                        t0 = time.perf_counter()
                        img_float = img_proc.astype(np.float32)
                        img_proc = np.clip(img_float, 0, 255).astype(np.uint8)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] float_convert: {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['float_convert'] += dt

                        # 5. Gaussian blur
                        t0 = time.perf_counter()
                        img_proc = cv2.GaussianBlur(img_float, (5, 5), 0)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] GaussianBlur (7x7): {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['GaussianBlur'] += dt

                        img_result = img_proc

                    elif pipeline_type == 'analysis':
                        # --- ANALYSIS PIPELINE ---
                        # 1. Convert to grayscale
                        t0 = time.perf_counter()
                        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] cvtColor (BGR→GRAY): {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['cvtColor'] += dt

                        # 2. CLAHE (Contrast Limited Adaptive Histogram Equalization)
                        t0 = time.perf_counter()
                        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                        enhanced = clahe.apply(gray)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] CLAHE: {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['CLAHE'] += dt

                        # 3. Calculate histogram
                        t0 = time.perf_counter()
                        hist = cv2.calcHist([enhanced], [0], None, [256], [0, 256])
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] calcHist: {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['calcHist'] += dt

                        # 4. HoughCircles detection
                        t0 = time.perf_counter()
                        blurred_for_hough = cv2.medianBlur(enhanced, 5)
                        circles = cv2.HoughCircles(
                            blurred_for_hough, cv2.HOUGH_GRADIENT, 1, 20, 
                            param1=50, param2=30, minRadius=0, maxRadius=0
                        )
                        dt = time.perf_counter() - t0
                        circle_count = 0 if circles is None else len(circles[0])
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] HoughCircles: {dt:.6f}s (found {circle_count} circles)")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['HoughCircles'] += dt

                        # 5. Find contours on thresholded image
                        t0 = time.perf_counter()
                        _, thresh = cv2.threshold(enhanced, 127, 255, cv2.THRESH_BINARY)
                        contours, _ = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] findContours: {dt:.6f}s (found {len(contours)} contours)")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['findContours'] += dt

                        # Draw results on color version for visualization
                        img_result = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)
                        if circles is not None:
                            circles = np.uint16(np.around(circles))
                            for circle in circles[0, :]:
                                cv2.circle(img_result, (circle[0], circle[1]), circle[2], (0, 255, 0), 2)
                        cv2.drawContours(img_result, contours, -1, (0, 0, 255), 1)

                    else:
                        # --- STANDARD PIPELINE (default) ---
                        # 1. Resize
                        h, w = img.shape[:2]
                        if w > h:
                            new_w, new_h = 512, int(512 * h / w)
                        else:
                            new_w, new_h = int(512 * w / h), 512

                        t0 = time.perf_counter()
                        img_resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] resize: {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['resize'] += dt

                        # 2. cvtColor
                        t0 = time.perf_counter()
                        gray = cv2.cvtColor(img_resized, cv2.COLOR_BGR2GRAY)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] cvtColor (BGR→GRAY): {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['cvtColor'] += dt

                        # 3. GaussianBlur
                        t0 = time.perf_counter()
                        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] GaussianBlur (5x5): {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['blur'] += dt

                        # 4. threshold
                        t0 = time.perf_counter()
                        _, thresh = cv2.threshold(
                            blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
                        )
                        dt = time.perf_counter() - t0
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] threshold (OTSU): {dt:.6f}s")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['threshold'] += dt

                        # 5. findContours
                        t0 = time.perf_counter()
                        contours, _ = cv2.findContours(
                            thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                        )
                        dt = time.perf_counter() - t0
                        contour_count = len(contours)
                        logger.info(f"⚡ [Iter {iteration+1}] [Img {idx+1}] findContours: {dt:.6f}s (found {contour_count} contours)")
                        opencv_total_time += dt
                        iteration_opencv_time += dt
                        if iteration == 0:
                            operation_times['findContours'] += dt

                        # Draw contours (not counted in timing)
                        cv2.drawContours(img_resized, contours, -1, (0, 255, 0), 2)
                        img_result = img_resized

                    # imencode (not counted in opencv_total_time for fairness)
                    ok, buffer = cv2.imencode('.jpg', img_result, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if not ok:
                        continue

                    images_processed_count += 1

                    if (idx + 1) % 10 == 0 and iteration == 0:
                        logger.info(f"Iteration {iteration+1}/{iterations}: Processed {idx+1}/{len(images_b64)} images")

                except Exception as e:
                    logger.error(f"Error processing image {idx}: {e}")
                    logger.error(traceback.format_exc())
                    continue

            iteration_wall_time = time.perf_counter() - iteration_wall_start
            wall_total_time += iteration_wall_time

            if iterations > 1:
                logger.info(
                    f"Iteration {iteration+1}/{iterations} completed: "
                    f"opencv={iteration_opencv_time:.4f}s, wall={iteration_wall_time:.4f}s"
                )

        # Log OpenCV-only timing breakdown
        logger.info(f"⏱️  OpenCV-only timing breakdown ({pipeline_type} pipeline):")
        op_total = sum(operation_times.values())
        if op_total > 0:
            for op, t in sorted(operation_times.items(), key=lambda x: x[1], reverse=True):
                logger.info(f"  {op}: {t:.6f}s ({(t / op_total) * 100:.1f}%)")
        else:
            for op in operation_times:
                logger.info(f"  {op}: 0.000000s (0.0%)")

        logger.info(
            f"✅ Completed processing {images_processed_count} images "
            f"({pipeline_type} pipeline) | "
            f"opencv_time={opencv_total_time:.6f}s | wall_time={wall_total_time:.6f}s"
        )

        cache_info = _get_cache_info()

        return web.json_response({
            # OpenCV-only time
            'processing_time': opencv_total_time,
            # Extra debug field
            'wall_time': wall_total_time,
            'processed_images': processed_images,
            'images_processed': images_processed_count,
            'pipeline_type': pipeline_type,
            'contours_detected': pipeline_type in ('standard', 'analysis'),
            'opencv_version': cv2.__version__ if OPENCV_AVAILABLE else 'N/A',
            'opencv_operation_times': operation_times,
            'memory_benchmark': MEMORY_BENCHMARK_RESULTS or {},
            'cache_info': cache_info
        })

    except Exception as e:
        logger.error(f"❌ Critical error in handle_process: {e}")
        logger.error(traceback.format_exc())
        return web.json_response({'error': str(e)}, status=500)

async def handle_health(request):
    """Health check endpoint"""
    logger.debug("Health check requested")
    return web.json_response({
        'status': 'healthy',
        'opencv_available': OPENCV_AVAILABLE,
        'opencv_version': cv2.__version__ if OPENCV_AVAILABLE else 'N/A',
        'timestamp': time.time()
    })

def create_app():
    """Create the web application"""
    app = web.Application(client_max_size=200*1024*1024)  # 200MB max request size
    app.router.add_post('/process', handle_process)
    app.router.add_get('/health', handle_health)
    return app

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Starting OpenCV MCP Server on 0.0.0.0:8080")
    logger.info(f"OpenCV Available: {OPENCV_AVAILABLE}")
    if OPENCV_AVAILABLE:
        logger.info(f"OpenCV Version: {cv2.__version__}")
    logger.info("=" * 60)
    
    try:
        app = create_app()
        # Use asyncio runner instead of web.run_app for better systemd compatibility
        async def run_server():
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', 8080)
            await site.start()
            logger.info("✅ Server started successfully on 0.0.0.0:8080")
            # Keep running forever
            await asyncio.Event().wait()
        
        asyncio.run(run_server())
    except Exception as e:
        logger.error(f"❌ Server failed to start: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)
