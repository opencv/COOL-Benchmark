#!/usr/bin/env python3
"""
Enhanced benchmark execution with real EC2 integration, build progress tracking,
and automatic cleanup with 1-hour timeout
"""

import asyncio
import logging
import time
import boto3
import aiohttp
from typing import Dict, List, Any, Optional

logger = logging.getLogger("benchmark-executor")

# Import MCP reconfiguration helpers for cross-mode instance reuse
from shared_instance_benchmark import reconfigure_mcp_for_diy, reconfigure_mcp_for_cool

# 1 hour timeout for all benchmarks
BENCHMARK_TIMEOUT_SECONDS = 3600

async def execute_benchmark_with_build(
    task,
    instance_manager,
    build_manager,
    orchestrator
):
    """
    Execute benchmark with real EC2 instance launch and build tracking
    Includes automatic cleanup and 1-hour timeout protection
    Supports parallel execution with multiple instances
    
    Args:
        task: BenchmarkTask object
        instance_manager: InstanceManager instance
        build_manager: BuildManager instance
        orchestrator: BenchmarkOrchestrator instance (for AMI IDs)
    """
    # Check if this is a parallel benchmark
    is_parallel = task.test_type == "parallel-graviton" and task.max_instances > 1
    
    if is_parallel:
        await _execute_parallel_benchmark(task, instance_manager, build_manager, orchestrator)
    else:
        await _execute_single_benchmark(task, instance_manager, build_manager, orchestrator)


async def _execute_single_benchmark(
    task,
    instance_manager,
    build_manager,
    orchestrator
):
    """Execute benchmark on a single instance"""
    instance_id = None
    start_time = time.time()
    
    try:
        task.status = "staging"  # Start with staging status during setup
        task.build_progress = {
            "current_step": "Initializing",
            "steps": [],
            "progress_percent": 0
        }
        
        logger.info(f"Executing benchmark {task.task_id}: {task.test_type} on {task.instance_type}")
        
        # Step 1: Determine AMI and architecture
        ami_id, architecture = await _determine_ami_and_arch(task, orchestrator)
        _check_timeout(start_time, "AMI determination")
        
        # Step 2: Check for reusable instance or launch new one
        task.build_progress["current_step"] = "Checking for reusable instance"
        task.build_progress["progress_percent"] = 10
        
        # Try to find a reusable instance with matching configuration
        instance_id, pool_build_mode = instance_manager.find_reusable_instance(
            task.instance_type,
            task.build_mode,
            architecture
        )
        
        if instance_id:
            logger.info(f"♻️ Reusing existing instance {instance_id} (pool_build_mode={pool_build_mode}, requested={task.build_mode})")
            task.build_progress["current_step"] = "Reusing existing instance (OpenCV already installed)"
            task.build_progress["progress_percent"] = 70  # Skip to 70% since build is done
            
            # Check if MCP server needs reconfiguration for a different build mode
            needs_reconfigure = (pool_build_mode != task.build_mode)
            skip_build = True
        else:
            logger.info(f"No reusable instance found, launching new instance")
            task.build_progress["current_step"] = "Launching new instance"
            
            instance_id = await _launch_instance(
                task, ami_id, architecture, instance_manager, build_manager
            )
            
            logger.info(f"Launched instance {instance_id} for task {task.task_id}")
            skip_build = False
            needs_reconfigure = False
        
        _check_timeout(start_time, "Instance launch")
        
        # Step 3: Install/Build OpenCV based on build_mode (skip if reusing instance)
        build_start_time = time.time()
        build_result = None  # Initialize to None for reused instances
        
        if not skip_build:
            task.build_progress["current_step"] = "Installing OpenCV"
            task.build_progress["progress_percent"] = 20
            
            logger.info(f"Installing OpenCV on {instance_id} using {task.build_mode} mode")
            
            build_result = await _install_opencv(
                task, instance_id, architecture, build_manager, orchestrator
            )
            
            build_duration = time.time() - build_start_time
            
            if build_result["status"] != "success":
                error_msg = f"Build failed: {build_result.get('error', 'Unknown error')}"
                logger.error(error_msg)
                
                # Record failed build attempt
                if orchestrator:
                    _record_build_attempt(orchestrator, architecture, task.build_mode, "failed", 
                                        build_duration, task.instance_type, error_msg)
                
                raise Exception(error_msg)
            
            logger.info(f"✅ OpenCV installation completed: {build_result.get('method')} in {build_result.get('duration', 0):.1f}s")
            
            # Record successful build attempt
            if orchestrator:
                _record_build_attempt(orchestrator, architecture, task.build_mode, "success", 
                                    build_duration, task.instance_type, None)
        else:
            logger.info(f"✅ Skipping OpenCV installation (reusing instance with existing installation)")
            # Create a placeholder build_result for reused instances
            build_result = {
                "status": "reused",
                "method": "reused",
                "duration": 0
            }
            
            # If the reused instance has a different build mode, reconfigure MCP
            if needs_reconfigure:
                logger.info(f"🔧 Reconfiguring MCP server: {pool_build_mode} → {task.build_mode}")
                task.build_progress["current_step"] = f"Reconfiguring MCP for {task.build_mode}"
                task.build_progress["progress_percent"] = 72
                
                if task.build_mode in ("pip", "compile"):
                    # Switching from COOL/marketplace → DIY
                    await reconfigure_mcp_for_diy(instance_id, build_manager)
                elif task.build_mode == "marketplace":
                    # Switching from DIY → COOL/marketplace
                    await reconfigure_mcp_for_cool(instance_id, build_manager)
                
                # Update the pool's build_mode to reflect the new state
                if instance_id in instance_manager.instance_pool:
                    instance_manager.instance_pool[instance_id]["build_mode"] = task.build_mode
                
                logger.info(f"✅ MCP reconfigured for {task.build_mode}")
        
        _check_timeout(start_time, "OpenCV installation")
        
        # NOW switch to running status when actually processing images
        task.status = "running"
        task.build_progress["current_step"] = "Running benchmark"
        task.build_progress["progress_percent"] = 80
        
        # Step 4: Run benchmark
        benchmark_results = await _run_benchmark_on_instance(
            task, instance_id, instance_manager
        )
        
        _check_timeout(start_time, "Benchmark execution")
        
        # Step 4.5: Fetch and clear logs from EC2 instance so next run has fresh logs
        try:
            logger.info(f"Fetching run logs from EC2 instance {instance_id}")
            log_result = await build_manager._execute_ssm_command(
                instance_id,
                ['cat /var/log/opencv-mcp.log', 'sudo truncate -s 0 /var/log/opencv-mcp.log'],
                timeout=60
            )
            if log_result['status'] == 'success':
                import os
                os.makedirs('logs', exist_ok=True)
                pipeline = getattr(task, 'pipeline_type', 'standard')
                log_filename = f"logs/{task.instance_type}_{task.build_mode}_{pipeline}_{task.task_id[:8]}.log"
                with open(log_filename, 'w', encoding='utf-8') as f:
                    f.write(log_result.get('stdout', ''))
                logger.info(f"💾 Saved EC2 processing logs to {log_filename}")
        except Exception as log_e:
            logger.warning(f"Could not fetch/clear EC2 logs: {log_e}")
        
        task.build_progress["current_step"] = "Completed"
        task.build_progress["progress_percent"] = 100
        
        # Step 5: Compile final results
        elapsed_time = time.time() - start_time
        total_images_processed = task.image_count * task.iterations  # Account for iterations
        
        # Calculate cost based on total elapsed time (not just processing time)
        instance_cost_per_hour = _get_instance_cost(task.instance_type)
        runtime_hours = elapsed_time / 3600
        total_cost = instance_cost_per_hour * runtime_hours
        
        task.results = {
            "duration": benchmark_results["processing_time"],
            "images_processed": total_images_processed,
            "throughput": total_images_processed / benchmark_results["processing_time"] if benchmark_results["processing_time"] > 0 else 0,
            "instances_used": 1,
            "cost": total_cost,  # Use calculated cost based on total elapsed time
            "build_info": build_result,
            "instance_type": task.instance_type,
            "build_mode": task.build_mode,
            "iterations": task.iterations,
            "total_elapsed_time": elapsed_time,
            "processed_images": benchmark_results.get("processed_images", [])[:20],
            "memory_benchmark": benchmark_results.get("memory_benchmark", {}),
            "cache_info": benchmark_results.get("cache_info", {})
        }
        
        task.status = "completed"
        task.end_time = time.time()
        
        logger.info(f"Benchmark task {task.task_id} completed successfully in {elapsed_time:.1f}s")
        
    except TimeoutError as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Benchmark task {task.task_id} timed out after {elapsed_time:.1f}s: {e}")
        task.status = "failed"
        task.error = f"Timeout after {elapsed_time:.0f}s: {str(e)}"
        task.end_time = time.time()
        
    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Error in benchmark task {task.task_id} after {elapsed_time:.1f}s: {e}")
        task.status = "failed"
        task.error = str(e)
        task.end_time = time.time()
        
        # Try to retrieve console logs before termination for debugging
        if instance_id:
            try:
                logger.info(f"Retrieving console logs from {instance_id} before termination...")
                ec2_client = boto3.client('ec2', region_name=instance_manager.region)
                response = ec2_client.get_console_output(InstanceId=instance_id)
                console_output = response.get('Output', '')
                if console_output:
                    # Store last 2000 chars of console output in error
                    task.error = f"{str(e)}\n\nConsole output (last 2000 chars):\n{console_output[-2000:]}"
                    logger.error(f"Console output retrieved: {len(console_output)} bytes")
            except Exception as log_error:
                logger.warning(f"Could not retrieve console logs: {log_error}")
    
    finally:
        # MODIFIED: Keep instance alive for reuse instead of terminating
        if instance_id:
            try:
                # Add instance to reuse pool
                instance_manager.add_instance_to_pool(
                    instance_id, 
                    task.instance_type, 
                    task.build_mode,
                    architecture
                )
                logger.info(f"Instance {instance_id} added to reuse pool (will auto-terminate after 2 hours of inactivity)")
            except Exception as pool_error:
                logger.error(f"Error adding instance to pool: {pool_error}")
                # If we can't add to pool, terminate it
                try:
                    logger.warning(f"Terminating instance {instance_id} due to pool error")
                    await instance_manager.terminate_instance(instance_id)
                except Exception as cleanup_error:
                    logger.critical(f"CRITICAL: Failed to terminate instance {instance_id}: {cleanup_error}")
                    logger.critical(f"MANUAL INTERVENTION REQUIRED: Terminate instance {instance_id} manually!")


async def _execute_parallel_benchmark(
    task,
    instance_manager,
    build_manager,
    orchestrator
):
    """Execute benchmark across multiple parallel instances"""
    instance_ids = []
    start_time = time.time()
    
    try:
        task.status = "staging"
        task.build_progress = {
            "current_step": "Initializing parallel benchmark",
            "steps": [],
            "progress_percent": 0
        }
        
        logger.info(f"🚀 Executing PARALLEL benchmark {task.task_id}: up to {task.max_instances} instances of {task.instance_type}")
        
        # Step 1: Determine AMI and architecture
        ami_id, architecture = await _determine_ami_and_arch(task, orchestrator)
        _check_timeout(start_time, "AMI determination")
        
        # Step 2: Calculate optimal number of instances based on workload
        # Rule: Use 1 instance per 200 images (minimum workload per instance)
        # This ensures each instance has meaningful work to do
        min_images_per_instance = 200
        total_workload = task.image_count * task.iterations
        optimal_instances = max(1, min(task.max_instances, (total_workload + min_images_per_instance - 1) // min_images_per_instance))
        
        logger.info(f"📊 Workload analysis: {task.image_count} images × {task.iterations} iterations = {total_workload} total images")
        logger.info(f"📊 Optimal instances: {optimal_instances} (max allowed: {task.max_instances}, min workload: {min_images_per_instance} images/instance)")
        
        if optimal_instances < task.max_instances:
            logger.info(f"⚡ Using {optimal_instances} instances instead of {task.max_instances} (workload doesn't justify more instances)")
        
        instances_to_launch = optimal_instances
        
        # Step 3: Check for reusable instances or launch new ones
        task.build_progress["current_step"] = f"Checking for reusable instances"
        task.build_progress["progress_percent"] = 10
        
        # Try to find reusable instances first
        reusable_instances = []
        for i in range(instances_to_launch):
            instance_id, _pool_bm = instance_manager.find_reusable_instance(
                task.instance_type,
                task.build_mode,
                architecture
            )
            if instance_id:
                reusable_instances.append(instance_id)
                logger.info(f"♻️ Found reusable instance {i+1}/{instances_to_launch}: {instance_id}")
        
        instances_to_create = instances_to_launch - len(reusable_instances)
        
        if reusable_instances:
            logger.info(f"♻️ Reusing {len(reusable_instances)} existing instances, launching {instances_to_create} new instances")
        else:
            logger.info(f"🚀 No reusable instances found, launching {instances_to_create} new instances")
        
        task.build_progress["current_step"] = f"Launching {instances_to_create} new instances (reusing {len(reusable_instances)})"
        task.build_progress["progress_percent"] = 15
        
        # Launch only the new instances needed
        launch_tasks = []
        for i in range(instances_to_create):
            launch_task = _launch_instance(
                task, ami_id, architecture, instance_manager, build_manager
            )
            launch_tasks.append(launch_task)
        
        if launch_tasks:
            new_instance_ids = await asyncio.gather(*launch_tasks)
            instance_ids = reusable_instances + list(new_instance_ids)
            logger.info(f"✅ Total instances ready: {len(instance_ids)} ({len(reusable_instances)} reused, {len(new_instance_ids)} new)")
        else:
            instance_ids = reusable_instances
            logger.info(f"✅ Using {len(instance_ids)} reused instances (no new instances needed)")
        
        _check_timeout(start_time, "Instance launches")
        
        # Step 4: Install OpenCV on new instances only (skip for reused instances)
        task.build_progress["current_step"] = f"Installing OpenCV on {instances_to_create} new instances"
        task.build_progress["progress_percent"] = 30
        
        build_start_time = time.time()
        
        if instances_to_create > 0:
            # Install OpenCV only on new instances (not reused ones)
            install_tasks = []
            for instance_id in instance_ids[len(reusable_instances):]:  # Only new instances
                install_task = _install_opencv(
                    task, instance_id, architecture, build_manager, orchestrator
                )
                install_tasks.append(install_task)
            
            build_results = await asyncio.gather(*install_tasks, return_exceptions=True)
            
            # Check if any installations failed
            for i, result in enumerate(build_results):
                instance_id = instance_ids[len(reusable_instances) + i]
                if isinstance(result, Exception):
                    raise Exception(f"Installation failed on instance {instance_id}: {result}")
                if result["status"] != "success":
                    raise Exception(f"Installation failed on instance {instance_id}: {result.get('error')}")
            
            build_duration = time.time() - build_start_time
            logger.info(f"✅ OpenCV installed on {instances_to_create} new instances in {build_duration:.1f}s")
        else:
            logger.info(f"✅ Skipping OpenCV installation (all instances reused with existing installations)")
            build_results = [{"status": "reused", "method": "reused", "duration": 0}]  # Placeholder for reused instances
        
        _check_timeout(start_time, "OpenCV installation")
        
        # Step 5: Run benchmark in parallel across all instances
        task.status = "running"
        task.build_progress["current_step"] = f"Running benchmark on {len(instance_ids)} instances"
        task.build_progress["progress_percent"] = 70
        
        logger.info(f"Running benchmark across {len(instance_ids)} instances...")
        
        # Distribute images across instances
        images_per_instance = task.image_count // len(instance_ids)
        remainder = task.image_count % len(instance_ids)
        
        # Get images from orchestrator's image collection
        # Note: task should have images stored in task.results["images"]
        if not hasattr(task, 'results') or not task.results or "images" not in task.results:
            raise Exception("No images found in task - images must be loaded before parallel execution")
        
        all_images = task.results["images"]
        logger.info(f"📊 Distributing {len(all_images)} images across {len(instance_ids)} instances ({images_per_instance} per instance, {remainder} remainder)")
        
        # Run benchmarks in parallel
        benchmark_tasks = []
        image_offset = 0
        
        for i, instance_id in enumerate(instance_ids):
            # Give remainder images to first instances
            instance_image_count = images_per_instance + (1 if i < remainder else 0)
            
            # Get slice of images for this instance
            instance_images = all_images[image_offset:image_offset + instance_image_count]
            image_offset += instance_image_count
            
            logger.info(f"📦 Instance {i+1}/{len(instance_ids)} ({instance_id}): {instance_image_count} images (offset {image_offset - instance_image_count} to {image_offset})")
            
            # Create a task-like object with the required attributes
            class InstanceTask:
                def __init__(self, parent_task, images, instance_num):
                    self.task_id = f"{parent_task.task_id}-{instance_num}"
                    self.image_count = len(images)
                    self.iterations = parent_task.iterations
                    self.test_type = parent_task.test_type
                    self.build_mode = parent_task.build_mode
                    self.instance_type = parent_task.instance_type
                    self.pipeline_type = getattr(parent_task, 'pipeline_type', 'standard')
                    self.results = {"images": images}
            
            instance_task = InstanceTask(task, instance_images, i)
            
            benchmark_task = _run_benchmark_on_instance(
                instance_task, instance_id, instance_manager
            )
            benchmark_tasks.append(benchmark_task)
        
        logger.info(f"⏳ Waiting for {len(benchmark_tasks)} parallel benchmark tasks to complete...")
        benchmark_results = await asyncio.gather(*benchmark_tasks, return_exceptions=True)
        logger.info(f"✅ All parallel benchmark tasks completed")
        
        # Step 5.5: Fetch logs from all instances
        logger.info(f"Fetching run logs from {len(instance_ids)} EC2 instances...")
        for i, instance_id in enumerate(instance_ids):
            if not isinstance(benchmark_results[i], Exception):
                try:
                    log_result = await build_manager._execute_ssm_command(
                        instance_id,
                        ['cat /var/log/opencv-mcp.log', 'sudo truncate -s 0 /var/log/opencv-mcp.log'],
                        timeout=60
                    )
                    if log_result['status'] == 'success':
                        import os
                        os.makedirs('logs', exist_ok=True)
                        pipeline = getattr(task, 'pipeline_type', 'standard')
                        log_filename = f"logs/{task.instance_type}_{task.build_mode}_{pipeline}_{task.task_id[:8]}_inst{i}.log"
                        with open(log_filename, 'w', encoding='utf-8') as f:
                            f.write(log_result.get('stdout', ''))
                        logger.info(f"💾 Saved EC2 processing logs to {log_filename}")
                except Exception as log_e:
                    logger.warning(f"Could not fetch/clear EC2 logs for {instance_id}: {log_e}")

        _check_timeout(start_time, "Benchmark execution")
        
        task.build_progress["current_step"] = "Completed"
        task.build_progress["progress_percent"] = 100
        
        # Step 6: Aggregate results from all instances
        # Filter out exceptions and count only successful results
        successful_results = []
        failed_count = 0
        
        for i, result in enumerate(benchmark_results):
            if isinstance(result, Exception):
                logger.error(f"Instance {i+1} failed with exception: {result}")
                failed_count += 1
            elif result and "processing_time" in result:
                successful_results.append(result)
                logger.info(f"Instance {i+1} completed successfully: {result.get('images_processed', 0)} images in {result['processing_time']:.2f}s")
            else:
                logger.warning(f"Instance {i+1} returned invalid result: {result}")
                failed_count += 1
        
        if not successful_results:
            raise Exception(f"All {len(instance_ids)} instances failed to complete benchmark")
        
        if failed_count > 0:
            logger.warning(f"⚠️ {failed_count}/{len(instance_ids)} instances failed, continuing with {len(successful_results)} successful instances")
        
        successful_instances = len(successful_results)
        
        total_processing_time = max(r["processing_time"] for r in successful_results)
        all_processed_images = []
        for result in successful_results:
            all_processed_images.extend(result.get("processed_images", []))
        
        # Take first 20 images for display
        all_processed_images = all_processed_images[:20]
        
        elapsed_time = time.time() - start_time
        total_images_processed = task.image_count * task.iterations
        
        # Calculate total cost based on elapsed time for all instances
        # Each instance runs for the full elapsed time
        instance_cost_per_hour = _get_instance_cost(task.instance_type)
        runtime_hours = elapsed_time / 3600
        total_cost = instance_cost_per_hour * runtime_hours * successful_instances
        
        # Get memory metrics from first successful instance
        memory_benchmark = {}
        cache_info = {}
        if benchmark_results:
            memory_benchmark = benchmark_results[0].get("memory_benchmark", {})
            cache_info = benchmark_results[0].get("cache_info", {})
        
        task.results = {
            "duration": total_processing_time,
            "images_processed": total_images_processed,
            "throughput": total_images_processed / total_processing_time if total_processing_time > 0 else 0,
            "instances_used": successful_instances,  # Only count instances that completed successfully
            "cost": total_cost,
            "build_info": build_results[0],  # Use first instance's build info
            "instance_type": task.instance_type,
            "build_mode": task.build_mode,
            "iterations": task.iterations,
            "total_elapsed_time": elapsed_time,
            "processed_images": all_processed_images,
            "memory_benchmark": memory_benchmark,
            "cache_info": cache_info
        }
        
        task.status = "completed"
        task.end_time = time.time()
        
        logger.info(f"✅ Parallel benchmark {task.task_id} completed: {successful_instances}/{len(instance_ids)} instances successful, {elapsed_time:.1f}s total, {total_processing_time:.1f}s processing")
        
    except TimeoutError as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Parallel benchmark {task.task_id} timed out after {elapsed_time:.1f}s: {e}")
        task.status = "failed"
        task.error = f"Timeout after {elapsed_time:.0f}s: {str(e)}"
        task.end_time = time.time()
        
    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Error in parallel benchmark {task.task_id} after {elapsed_time:.1f}s: {e}")
        task.status = "failed"
        task.error = str(e)
        task.end_time = time.time()
    
    finally:
        # Add all instances to reuse pool and release them
        for instance_id in instance_ids:
            if instance_id:
                try:
                    instance_manager.add_instance_to_pool(
                        instance_id, 
                        task.instance_type, 
                        task.build_mode,
                        architecture
                    )
                    # Release the instance back to idle state
                    instance_manager.release_instance(instance_id)
                    logger.info(f"Instance {instance_id} added to reuse pool and released to idle state")
                except Exception as pool_error:
                    logger.error(f"Error adding instance {instance_id} to pool: {pool_error}")
                    try:
                        await instance_manager.terminate_instance(instance_id)
                    except Exception as cleanup_error:
                        logger.critical(f"CRITICAL: Failed to terminate instance {instance_id}: {cleanup_error}")


def _check_timeout(start_time: float, step_name: str):
    """Check if benchmark has exceeded timeout"""
    elapsed = time.time() - start_time
    if elapsed > BENCHMARK_TIMEOUT_SECONDS:
        raise TimeoutError(f"Benchmark exceeded 1 hour timeout during {step_name} (elapsed: {elapsed:.0f}s)")


async def _determine_ami_and_arch(task, orchestrator) -> tuple:
    """Determine which AMI to use and architecture"""
    
    if task.test_type == "optimized-graviton":
        # Use marketplace AMI for Graviton2, fallback to base if not configured
        if orchestrator.marketplace_ami_id and not orchestrator.marketplace_ami_id.startswith("ami-to-be"):
            return orchestrator.marketplace_ami_id, "arm64"
        else:
            logger.warning("Marketplace AMI not configured, falling back to base ARM64 AMI with compile mode")
            task.build_mode = "compile"  # Force compile mode for optimization
            return orchestrator.base_arm64_ami_id, "arm64"
    
    if task.test_type == "unoptimized-graviton":
        # Use marketplace AMI for DIY Graviton too — enables same-instance reuse
        # with COOL benchmarks (MCP will be reconfigured for pip OpenCV)
        if orchestrator.marketplace_ami_id and not orchestrator.marketplace_ami_id.startswith("ami-to-be"):
            return orchestrator.marketplace_ami_id, "arm64"
        else:
            return orchestrator.base_arm64_ami_id, "arm64"
    
    elif task.test_type == "unoptimized-x86":
        # Use base x86_64 AMI
        return orchestrator.base_x86_ami_id, "x86_64"
    
    elif task.test_type == "parallel-graviton":
        # Use marketplace AMI for parallel tests, fallback to base
        if orchestrator.marketplace_ami_id and not orchestrator.marketplace_ami_id.startswith("ami-to-be"):
            return orchestrator.marketplace_ami_id, "arm64"
        else:
            logger.warning("Marketplace AMI not configured, falling back to base ARM64 AMI")
            return orchestrator.base_arm64_ami_id, "arm64"
    
    else:
        raise ValueError(f"Unknown test type: {task.test_type}")


async def _launch_instance(task, ami_id, architecture, instance_manager, build_manager):
    """Launch EC2 instance with appropriate user data"""
    
    logger.info(f"🚀 Launching EC2 instance: type={task.instance_type}, ami={ami_id}, arch={architecture}")
    
    # Get user data script based on build mode
    user_data = build_manager.get_user_data_script(task.build_mode, architecture)
    
    # Launch instance via instance manager
    instance_id = await instance_manager.launch_instance(
        instance_type=task.instance_type,
        ami_id=ami_id,
        user_data=user_data,
        tags={
            "Name": f"opencv-benchmark-{task.task_id[:8]}",
            "BenchmarkTaskId": task.task_id,
            "TestType": task.test_type,
            "BuildMode": task.build_mode,
            "AutoTerminate": "true",  # Mark for automatic cleanup
            "MaxLifetime": "3600"  # 1 hour max
        }
    )
    
    logger.info(f"✅ Instance launched: {instance_id}")
    logger.info(f"⏳ Waiting for instance {instance_id} to be running...")
    
    # Wait for instance to be running
    await instance_manager.wait_for_instance_ready(instance_id)
    
    logger.info(f"✅ Instance {instance_id} is running and ready")
    
    return instance_id


async def _install_opencv(task, instance_id, architecture, build_manager, orchestrator=None):
    """Install or compile OpenCV based on build mode"""
    
    if task.build_mode == "marketplace":
        # Marketplace AMI already has OpenCV
        logger.info(f"Using marketplace AMI for {instance_id}")
        task.build_progress["current_step"] = "Using marketplace AMI"
        license_key = orchestrator.marketplace_license_key if orchestrator else None
        result = await build_manager.use_marketplace_ami(instance_id, license_key)
        
        # Log the result
        if result["status"] == "success":
            logger.info(f"✅ Marketplace AMI ready with MCP server in {result['duration']:.1f}s")
            task.build_progress["current_step"] = "Marketplace AMI ready (MCP server included)"
        else:
            logger.error(f"❌ Marketplace AMI setup failed on {instance_id}: {result.get('error')}")
            task.build_progress["current_step"] = f"Marketplace setup failed: {result.get('error')}"
        
        # NOTE: For marketplace mode, MCP server is deployed via user-data script (no SSM required)
        return result
        
    elif task.build_mode == "pip":
        # Quick pip install
        logger.info(f"Starting pip install on {instance_id}")
        task.build_progress["current_step"] = "Installing OpenCV via pip (~10 min)"
        result = await build_manager.install_opencv_pip(instance_id, architecture)
        
        # Log the result
        if result["status"] == "success":
            logger.info(f"✅ OpenCV pip install completed successfully on {instance_id}")
            logger.info(f"🎉 Installation took {result['duration']:.1f}s - OpenCV is ready!")
            task.build_progress["current_step"] = "OpenCV installed successfully (MCP server included)"
            
            # Add a brief pause to show the success message
            await asyncio.sleep(2)
            
            if "stdout" in result:
                logger.info(f"Installation output: {result['stdout'][-200:]}")  # Last 200 chars
        else:
            logger.error(f"❌ OpenCV pip install failed on {instance_id}: {result.get('error')}")
            task.build_progress["current_step"] = f"Installation failed: {result.get('error')}"
            if "stderr" in result:
                logger.error(f"Error output: {result['stderr']}")
        
        # NOTE: For pip mode, MCP server is deployed via user-data script (no SSM required)
        # For compile/marketplace modes, MCP server is deployed separately below
        return result
        
    elif task.build_mode == "compile":
        # Full compilation with progress tracking
        logger.info(f"Starting compilation on {instance_id}")
        task.build_progress["current_step"] = "Compiling OpenCV from source (~30-45 min)"
        
        # Start compilation
        compile_task = asyncio.create_task(
            build_manager.compile_opencv_from_source(instance_id, architecture)
        )
        
        # Poll for progress while compiling
        while not compile_task.done():
            progress = await build_manager.get_build_progress(instance_id)
            task.build_progress["current_step"] = progress.get("current_step", "Compiling")
            task.build_progress["progress_percent"] = 20 + (progress.get("progress", 0) * 0.6)  # 20-80%
            
            await asyncio.sleep(10)  # Check every 10 seconds
        
        result = await compile_task
        
        # Log the result
        if result["status"] == "success":
            logger.info(f"✅ OpenCV compilation completed successfully on {instance_id}")
            logger.info(f"Compilation took {result['duration']:.1f}s")
            task.build_progress["current_step"] = "OpenCV compiled successfully"
        else:
            logger.error(f"❌ OpenCV compilation failed on {instance_id}: {result.get('error')}")
            task.build_progress["current_step"] = f"Compilation failed: {result.get('error')}"
    
    else:
        raise ValueError(f"Unknown build mode: {task.build_mode}")
    
    # Update task with build steps
    if "build_steps" in result:
        task.build_progress["steps"] = result["build_steps"]
    
    # Deploy MCP server after OpenCV installation
    # For pip mode, the user-data script already deploys via systemd — skip redundant SSM deployment
    if result["status"] == "success" and task.build_mode != "pip":
        logger.info(f"Deploying MCP server to {instance_id}")
        task.build_progress["current_step"] = "Deploying MCP server to EC2"
        mcp_result = await build_manager._deploy_mcp_server(instance_id)
        
        if mcp_result["status"] == "success":
            logger.info(f"✅ MCP server deployed successfully on {instance_id}")
            task.build_progress["current_step"] = "MCP server deployed successfully"
        else:
            logger.warning(f"⚠️ MCP server deployment had issues: {mcp_result.get('error')}")
            task.build_progress["current_step"] = f"MCP server deployment warning: {mcp_result.get('error')}"
            # Don't fail the whole build, just log the warning
    elif result["status"] == "success":
        logger.info(f"✅ MCP server already deployed via user-data on {instance_id}")
    
    return result


async def _run_benchmark_on_instance(task, instance_id, instance_manager):
    """Run the actual benchmark on EC2 via HTTP to MCP server"""
    
    # Get instance details
    instance = instance_manager.instances.get(instance_id)
    if not instance:
        raise Exception(f"Instance {instance_id} not found")
    
    try:
        import aiohttp
        
        # Get images from task
        images_b64 = task.results.get("images", [])
        if not images_b64:
            raise Exception("No images found in task")
        
        # Use public IP if available, otherwise private IP
        target_ip = instance.public_ip if instance.public_ip else instance.private_ip
        if not target_ip:
            raise Exception(f"Instance {instance_id} has no accessible IP address")
        
        logger.info(f"🔗 [{task.task_id}] Connecting to MCP server on {target_ip} for {len(images_b64)} images")
        
        # Wait for MCP server to be ready
        max_retries = 60  # Increased from 30 to 60 (10 minutes total)
        retry_interval = 10  # 10 seconds between retries
        mcp_ready = False
        for i in range(max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://{target_ip}:8080/health",
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as response:
                        if response.status == 200:
                            health = await response.json()
                            logger.info(f"✅ [{task.task_id}] MCP server ready on {target_ip} after {(i+1) * retry_interval}s: {health}")
                            mcp_ready = True
                            break
            except Exception as e:
                if i < max_retries - 1:
                    elapsed = (i + 1) * retry_interval
                    if i % 6 == 0:  # Log every minute
                        logger.info(f"⏳ [{task.task_id}] Waiting for MCP server on {target_ip}... ({elapsed}s elapsed, {max_retries * retry_interval - elapsed}s remaining)")
                    await asyncio.sleep(retry_interval)
                else:
                    # Get console output for debugging
                    try:
                        ec2_client = boto3.client('ec2', region_name=instance_manager.region)
                        response = ec2_client.get_console_output(InstanceId=instance_id)
                        console_output = response.get('Output', '')
                        
                        # Check if installation actually succeeded
                        if 'OpenCV installation complete' not in console_output:
                            raise Exception(f"OpenCV installation did not complete successfully. MCP server not ready after {max_retries * retry_interval}s: {e}\n\nConsole output (last 2000 chars):\n{console_output[-2000:]}")
                        else:
                            raise Exception(f"OpenCV installed but MCP server failed to start after {max_retries * retry_interval}s: {e}\n\nConsole output (last 2000 chars):\n{console_output[-2000:]}")
                    except Exception as console_error:
                        raise Exception(
                            (
                                f"MCP server health endpoint http://{target_ip}:8080/health "
                                f"was not reachable after {max_retries * retry_interval}s. "
                                "This usually indicates a networking issue (security group, NACL, or host firewall) "
                                "even if OpenCV installed correctly inside the instance. "
                                f"Last connection error: {e} | Console check error: {console_error}"
                            )
                        )
        
        if not mcp_ready:
            raise Exception(
                (
                    f"MCP server health endpoint http://{target_ip}:8080/health was not reachable "
                    f"after {max_retries * retry_interval}s. "
                    "Verify that the EC2 instance security group and network ACLs allow inbound TCP 8080 "
                    "from the orchestrator, and that the opencv-mcp systemd service is running."
                )
            )
        
        # Call MCP server to process images
        start_time = time.time()
        
        logger.info(f"📤 [{task.task_id}] Sending POST request to MCP server with {len(images_b64)} images × {task.iterations} iterations...")
        payload_size_mb = sum(len(img) for img in images_b64) / 1024 / 1024
        logger.info(f"📦 [{task.task_id}] Payload size: {payload_size_mb:.2f} MB")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://{target_ip}:8080/process",
                    json={
                        "images": images_b64,
                        "operation": "findContours",
                        "iterations": task.iterations,
                        "build_mode": task.build_mode,
                        "pipeline_type": getattr(task, 'pipeline_type', 'standard')
                    },
                    timeout=aiohttp.ClientTimeout(total=3600)  # 1 hour timeout
                ) as response:
                    
                    if response.status != 200:
                        error_text = await response.text()
                        raise Exception(f"MCP server returned status {response.status}: {error_text}")
                    
                    logger.info(f"📥 [{task.task_id}] Received response from MCP server, parsing JSON...")
                    result = await response.json()
                    logger.info(f"✅ [{task.task_id}] Successfully parsed response: {result.get('images_processed', 0)} images processed in {result.get('processing_time', 0):.2f}s")
        except asyncio.TimeoutError as e:
            logger.error(f"⏱️ [{task.task_id}] Timeout while processing images on MCP server: {e}")
            raise Exception(f"Image processing timed out after 1 hour")
        except aiohttp.ClientError as e:
            logger.error(f"🌐 [{task.task_id}] Network error while communicating with MCP server: {e}")
            raise Exception(f"Network error: {str(e)}")
        except Exception as e:
            logger.error(f"❌ [{task.task_id}] Unexpected error during image processing: {e}")
            raise
        
        processing_time = result.get('processing_time', time.time() - start_time)
        
        logger.info(f"Processed {result.get('images_processed', 0)} images on EC2 in {processing_time:.2f}s")
        
        return {
            "processing_time": processing_time,
            "processed_images": result.get("processed_images", []),
            "images_processed": result.get("images_processed", 0),
            "contours_detected": result.get("contours_detected", False),
            "opencv_version": result.get("opencv_version", "unknown"),
            "memory_benchmark": result.get("memory_benchmark", {}),
            "cache_info": result.get("cache_info", {})
        }
        
    except Exception as e:
        logger.error(f"Error running benchmark on instance: {e}")
        raise


def _get_instance_cost(instance_type: str) -> float:
    """Get hourly cost for instance type (2026 us-east-1 on-demand rates)"""
    
    costs = {
        # Graviton2
        "m6g.large": 0.077,
        "m6g.xlarge": 0.154,
        "m6g.2xlarge": 0.308,
        "c6g.large": 0.068,
        "c6g.xlarge": 0.136,
        
        # Graviton3
        "m7g.large": 0.082,    # Updated 2026
        "m7g.xlarge": 0.163,   # Updated 2026
        "c7g.large": 0.0725,
        "c7g.xlarge": 0.145,
        
        # Graviton4 
        "m8g.large": 0.0902,    # Updated 2026
        "m8g.xlarge": 0.1804,   # Updated 2026
        "c8g.large": 0.0798,    # Updated 2026
        "c8g.xlarge": 0.1596,   # Updated 2026

        # x86
        "m7i.large": 0.101,    # Updated 2026
        "m7i.xlarge": 0.202,   # Updated 2026
        "m6i.large": 0.096,
        "c7i.large": 0.085,
        "c6i.large": 0.085,
    }
    
    return costs.get(instance_type, 0.10)  # Default to $0.10/hour



def _record_build_attempt(orchestrator, architecture: str, build_mode: str, status: str, 
                         duration: float, instance_type: str, error: Optional[str] = None):
    """Record a build attempt in the orchestrator's history"""
    from dataclasses import dataclass
    import time
    
    # Import BuildAttempt from orchestrator module
    # We'll create the object directly since it's a dataclass
    key = f"{architecture}_{build_mode}"
    
    build_attempt = {
        "architecture": architecture,
        "build_mode": build_mode,
        "status": status,
        "duration": duration,
        "timestamp": time.time(),
        "instance_type": instance_type,
        "error": error
    }
    
    orchestrator.build_history[key] = build_attempt
    logger.info(f"📝 Recorded {status} build attempt: {architecture}/{build_mode} in {duration:.1f}s")
