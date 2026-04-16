#!/usr/bin/env python3
"""
Auto-Retry Manager - Handles automatic retry of failed installations with LLM-powered error analysis
"""

import asyncio
import json
import logging
import time
import re
import threading
from typing import Dict, Any, Optional
from dataclasses import dataclass
import anthropic

logger = logging.getLogger("auto-retry-manager")

@dataclass
class RetryAttempt:
    attempt_number: int
    error_message: str
    console_output: str
    script_modifications: str
    timestamp: float

class AutoRetryManager:
    def __init__(self, build_manager, instance_manager, orchestrator):
        self.build_manager = build_manager
        self.instance_manager = instance_manager
        self.orchestrator = orchestrator
        self.active_retries: Dict[str, Dict[str, Any]] = {}
        self.active_threads: Dict[str, threading.Thread] = {}  # Store thread references
        
    async def start_auto_retry(
        self,
        task_id: str,
        test_type: str,
        instance_type: str,
        build_mode: str,
        max_retries: int,
        claude_api_key: str
    ) -> Dict[str, Any]:
        """Start an auto-retry task"""
        
        logger.info(f"=== START AUTO-RETRY: task_id={task_id}, test_type={test_type}, instance_type={instance_type}, build_mode={build_mode} ===")
        
        self.active_retries[task_id] = {
            "status": "running",
            "test_type": test_type,
            "instance_type": instance_type,
            "build_mode": build_mode,
            "max_retries": max_retries,
            "attempt": 0,
            "current_step": "Initializing",
            "last_error": None,
            "attempts": [],
            "claude_api_key": claude_api_key,
            "start_time": time.time()  # Track total elapsed time
        }
        
        # Start the retry loop in a background thread
        logger.info(f"Creating background thread for retry loop...")
        print(f"========== CREATING THREAD for {task_id} ==========", flush=True)
        
        def _thread_wrapper():
            """Wrapper to run async code in thread"""
            try:
                print(f"========== THREAD STARTED for {task_id} ==========", flush=True)
                # Create new event loop for this thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._retry_loop(task_id))
                finally:
                    loop.close()
            except Exception as e:
                print(f"========== EXCEPTION IN THREAD: {e} ==========", flush=True)
                logger.error(f"FATAL ERROR in retry thread: {e}", exc_info=True)
                retry_info = self.active_retries.get(task_id)
                if retry_info:
                    retry_info["status"] = "failed"
                    retry_info["error"] = f"Fatal error: {str(e)}"
                    retry_info["current_step"] = f"Fatal error: {str(e)}"
            finally:
                # Clean up thread reference when done
                if task_id in self.active_threads:
                    del self.active_threads[task_id]
                    print(f"========== THREAD CLEANED UP: {task_id} ==========", flush=True)
        
        # Create and start thread
        thread = threading.Thread(target=_thread_wrapper, daemon=True, name=f"auto-retry-{task_id[:8]}")
        thread.start()
        
        # Store the thread reference
        self.active_threads[task_id] = thread
        
        print(f"========== THREAD CREATED: {thread.name} ==========", flush=True)
        logger.info(f"Background thread created: {thread.name}, returning to caller")
        
        return {"task_id": task_id, "status": "started"}
    
    async def _retry_loop(self, task_id: str):
        """Main retry loop that attempts installation until success or max retries"""
        print(f"========== RETRY LOOP STARTED for task {task_id} ==========", flush=True)
        logger.info(f"=== RETRY LOOP STARTED for task {task_id} ===")
        retry_info = self.active_retries[task_id]
        print(f"Retrieved retry_info: {retry_info}", flush=True)
        
        try:
            logger.info(f"Starting retry loop with max_retries={retry_info['max_retries']}")
            print(f"About to start for loop with max_retries={retry_info['max_retries']}", flush=True)
            for attempt in range(1, retry_info["max_retries"] + 1):
                print(f"========== LOOP ITERATION {attempt} STARTED ==========", flush=True)
                retry_info["attempt"] = attempt
                retry_info["current_step"] = f"Attempt {attempt}: Launching instance"
                print(f"Updated retry_info: attempt={attempt}, current_step={retry_info['current_step']}", flush=True)
                
                logger.info(f"=== AUTO-RETRY ATTEMPT {attempt}/{retry_info['max_retries']} for {task_id} ===")
                
                # Determine architecture
                architecture = "arm64" if "g." in retry_info["instance_type"] else "x86_64"
                logger.info(f"Architecture determined: {architecture} for instance type {retry_info['instance_type']}")
                print(f"Architecture: {architecture}", flush=True)
                
                # Get appropriate AMI from orchestrator
                ami_id = self.orchestrator.base_arm64_ami_id if architecture == "arm64" else self.orchestrator.base_x86_ami_id
                print(f"AMI ID: {ami_id}", flush=True)
                
                if not ami_id:
                    error_msg = f"No base AMI configured for {architecture}"
                    logger.error(f"FATAL: {error_msg}")
                    print(f"========== FATAL: {error_msg} ==========", flush=True)
                    retry_info["status"] = "failed"
                    retry_info["error"] = error_msg
                    return
                
                logger.info(f"Using AMI {ami_id} for {architecture}")
                
                # Get user data script (will be modified if this is a retry)
                user_data = self._get_user_data_script(retry_info, attempt)
                print(f"Got user_data script, length: {len(user_data)}", flush=True)
                
                # Launch instance
                instance_id = None
                try:
                    logger.info(f"Launching {retry_info['instance_type']} instance...")
                    print(f"========== ABOUT TO LAUNCH INSTANCE ==========", flush=True)
                    instance_id = await self.instance_manager.launch_instance(
                        instance_type=retry_info["instance_type"],
                        ami_id=ami_id,
                        user_data=user_data,
                        tags={
                            "Name": f"opencv-auto-retry-{task_id[:8]}",
                            "AutoRetry": "true",
                            "RetryAttempt": str(attempt),
                            "TaskId": task_id,
                            "BuildMode": retry_info["build_mode"],
                            "TestType": retry_info["test_type"]
                        }
                    )
                    print(f"========== INSTANCE LAUNCHED: {instance_id} ==========", flush=True)
                    
                    retry_info["current_step"] = f"Attempt {attempt}: Installing OpenCV"
                    logger.info(f"Launched instance {instance_id} for retry attempt {attempt}")
                    
                    # Wait for installation to complete or fail
                    print(f"========== CALLING _wait_for_installation for attempt {attempt} ==========", flush=True)
                    logger.info(f"About to call _wait_for_installation for attempt {attempt}")
                    result = await self._wait_for_installation(instance_id, retry_info, attempt)
                    print(f"========== INSTALLATION RESULT: {result.get('status')} ==========", flush=True)
                    logger.info(f"_wait_for_installation returned: {result.get('status')}")
                    
                    if result["status"] == "success":
                        # Success! Calculate total time
                        total_elapsed = time.time() - retry_info["start_time"]
                        total_minutes = int(total_elapsed / 60)
                        total_seconds = int(total_elapsed % 60)
                        
                        retry_info["status"] = "success"
                        retry_info["current_step"] = f"✅ Installation succeeded on attempt {attempt} (Total time: {total_minutes}m {total_seconds}s)"
                        retry_info["total_time_minutes"] = total_minutes
                        retry_info["total_time_seconds"] = total_seconds
                        
                        logger.info(f"✅ Auto-retry succeeded on attempt {attempt} after {total_minutes}m {total_seconds}s")
                        
                        # Terminate the instance (we only needed to test installation)
                        await self.instance_manager.terminate_instance(instance_id)
                        return
                    
                    else:
                        # Failed - analyze error and prepare for next attempt
                        retry_info["last_error"] = result.get("error", "Unknown error")
                        logger.error(f"Attempt {attempt} failed: {retry_info['last_error']}")
                        
                        # Store attempt details
                        attempt_record = RetryAttempt(
                            attempt_number=attempt,
                            error_message=result.get("error", ""),
                            console_output=result.get("stderr", ""),
                            script_modifications="",
                            timestamp=time.time()
                        )
                        retry_info["attempts"].append(attempt_record)
                        
                        # Terminate failed instance BEFORE analyzing/launching next
                        logger.info(f"Terminating failed instance {instance_id}")
                        await self.instance_manager.terminate_instance(instance_id)
                        
                        if attempt < retry_info["max_retries"]:
                            # Analyze error with Claude and prepare next attempt
                            retry_info["current_step"] = f"🤖 Asking Claude AI to analyze error from attempt {attempt}..."
                            await self._analyze_and_fix_error(retry_info, result)
                            
                            # Show Claude's analysis in the status
                            if "last_analysis" in retry_info and retry_info["last_analysis"]:
                                retry_info["current_step"] = f"✨ Claude suggested {len(retry_info.get('suggested_fixes', []))} fixes - preparing attempt {attempt + 1}"
                            
                            # Wait a bit before next attempt
                            await asyncio.sleep(5)
                        
                except Exception as e:
                    logger.error(f"Error in retry attempt {attempt}: {e}", exc_info=True)
                    retry_info["last_error"] = str(e)
                    retry_info["current_step"] = f"Attempt {attempt}: Error - {str(e)}"
                    
                    # Ensure instance is terminated on exception
                    if instance_id:
                        try:
                            logger.info(f"Cleaning up instance {instance_id} after error")
                            await self.instance_manager.terminate_instance(instance_id)
                        except Exception as cleanup_error:
                            logger.error(f"Failed to cleanup instance {instance_id}: {cleanup_error}")
                    
                    if attempt >= retry_info["max_retries"]:
                        break
            
            # All retries exhausted - calculate total time
            total_elapsed = time.time() - retry_info["start_time"]
            total_minutes = int(total_elapsed / 60)
            total_seconds = int(total_elapsed % 60)
            
            retry_info["status"] = "failed"
            retry_info["current_step"] = f"❌ Failed after {retry_info['max_retries']} attempts (Total time: {total_minutes}m {total_seconds}s)"
            retry_info["error"] = retry_info["last_error"]
            retry_info["total_time_minutes"] = total_minutes
            retry_info["total_time_seconds"] = total_seconds
            
            logger.error(f"❌ Auto-retry failed after {retry_info['max_retries']} attempts and {total_minutes}m {total_seconds}s: {retry_info['error']}")
            
        except Exception as e:
            logger.error(f"Fatal error in retry loop: {e}", exc_info=True)
            retry_info["status"] = "failed"
            retry_info["error"] = str(e)
            retry_info["current_step"] = f"Fatal error: {str(e)}"
    
    async def _wait_for_installation(self, instance_id: str, retry_info: Dict, attempt: int) -> Dict[str, Any]:
        """Wait for installation to complete and return result"""
        
        # Store installation start time for progress tracking
        attempt_start_time = time.time()
        retry_info["installation_start_time"] = attempt_start_time
        retry_info["current_attempt"] = attempt
        retry_info["last_attempt_start_time"] = attempt_start_time  # Keep this even after attempt completes
        logger.info(f"Set installation_start_time for attempt {attempt}")
        
        try:
            # Use the build manager's installation method
            if retry_info["build_mode"] == "pip":
                result = await self.build_manager.install_opencv_pip(instance_id)
            elif retry_info["build_mode"] == "compile":
                architecture = "arm64" if "g." in retry_info["instance_type"] else "x86_64"
                result = await self.build_manager.compile_opencv_from_source(instance_id, architecture)
            else:
                result = {"status": "failed", "error": "Unknown build mode"}
            
            return result
        finally:
            # Clear installation start time but keep last_attempt_start_time for status display
            logger.info(f"Clearing installation_start_time for attempt {attempt}")
            retry_info.pop("installation_start_time", None)
    
    async def _analyze_and_fix_error(self, retry_info: Dict, result: Dict):
        """Use Claude API to analyze the error and suggest fixes"""
        
        try:
            logger.info(f"🤖 Calling Claude API to analyze error...")
            client = anthropic.Anthropic(api_key=retry_info["claude_api_key"])
            
            # Prepare the error context
            error_message = result.get("error", "Unknown error")
            console_output = result.get("stderr", "")[-2000:]  # Last 2000 chars
            
            # Get current script
            current_script = self._get_user_data_script(retry_info, retry_info["attempt"])
            
            prompt = f"""You are an expert DevOps engineer helping to fix a failed OpenCV installation on an AWS EC2 instance.

Build Mode: {retry_info["build_mode"]}
Instance Type: {retry_info["instance_type"]}
Attempt: {retry_info["attempt"]}/{retry_info["max_retries"]}

Error Message:
{error_message}

Console Output (last 2000 chars):
{console_output}

Current Installation Script:
```bash
{current_script}
```

Please analyze the error and provide:
1. A brief explanation of what went wrong
2. Specific fixes to apply to the bash script
3. The complete modified bash script

Format your response as JSON:
{{
    "analysis": "brief explanation",
    "fixes": ["fix 1", "fix 2"],
    "modified_script": "complete bash script here"
}}
"""
            
            message = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=4000,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            # Parse Claude's response
            response_text = message.content[0].text
            
            # Extract JSON from response (Claude might wrap it in markdown)
            json_match = re.search(r'```json\s*(.*?)\s*```', response_text, re.DOTALL)
            if json_match:
                response_json = json.loads(json_match.group(1))
            else:
                # Try parsing the whole response as JSON
                response_json = json.loads(response_text)
            
            # Store the analysis and modified script
            retry_info["last_analysis"] = response_json.get("analysis", "")
            retry_info["suggested_fixes"] = response_json.get("fixes", [])
            retry_info["modified_script"] = response_json.get("modified_script", current_script)
            
            logger.info(f"✅ Claude analysis complete")
            logger.info(f"📋 Analysis: {response_json.get('analysis', '')}")
            logger.info(f"🔧 Suggested fixes ({len(retry_info['suggested_fixes'])}): {response_json.get('fixes', [])}")
            
        except Exception as e:
            logger.error(f"❌ Error analyzing with Claude: {e}")
            # Continue with original script if analysis fails
            retry_info["last_analysis"] = f"Analysis failed: {e}"
            retry_info["suggested_fixes"] = []
    
    def _get_user_data_script(self, retry_info: Dict, attempt: int) -> str:
        """Get the user data script, using modified version if available"""
        
        # If we have a modified script from Claude, use it
        if attempt > 1 and "modified_script" in retry_info:
            logger.info(f"✨ Using Claude-modified script for attempt {attempt}")
            return retry_info["modified_script"]
        
        # Otherwise, get the default script from build manager
        logger.info(f"📝 Using default script for attempt {attempt}")
        if retry_info["build_mode"] == "pip":
            return self.build_manager._get_pip_user_data()
        elif retry_info["build_mode"] == "compile":
            architecture = "arm64" if "g." in retry_info["instance_type"] else "x86_64"
            return self.build_manager._get_compile_user_data(architecture)
        else:
            return self.build_manager._get_marketplace_user_data()
    
    def get_retry_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get the status of an auto-retry task with dynamic elapsed time"""
        retry_info = self.active_retries.get(task_id)
        if not retry_info:
            logger.warning(f"No retry_info found for task_id: {task_id}")
            return None
        
        # Log current state for debugging
        logger.info(f"get_retry_status: attempt={retry_info.get('attempt')}, has_start_time={'installation_start_time' in retry_info}")
        
        # Calculate total elapsed time since auto-retry started
        total_elapsed = time.time() - retry_info["start_time"]
        total_elapsed_min = int(total_elapsed / 60)
        
        # Calculate elapsed time for current attempt
        # Use installation_start_time if currently installing, otherwise use last_attempt_start_time
        attempt_start = retry_info.get("installation_start_time") or retry_info.get("last_attempt_start_time")
        if attempt_start:
            elapsed = time.time() - attempt_start
            elapsed_min = int(elapsed / 60)
            elapsed_sec = int(elapsed % 60)
            attempt = retry_info.get("current_attempt", retry_info.get("attempt", 0))
            
            # Only update the step if we're in progress (not showing analysis/fixes)
            current_step = retry_info.get("current_step", "")
            if "Installing OpenCV" in current_step or "Attempt" in current_step:
                # Show minutes and seconds for better granularity
                if elapsed_min > 0:
                    time_str = f"{elapsed_min}m {elapsed_sec}s"
                else:
                    time_str = f"{elapsed_sec}s"
                new_step = f"Attempt {attempt}: Installing OpenCV ({time_str} elapsed)"
                retry_info["current_step"] = new_step
                logger.info(f"Updated current_step: {new_step}")
        else:
            logger.info(f"No attempt start time, current_step: {retry_info.get('current_step')}")
        
        # Create response with Claude analysis info
        response = {
            "status": retry_info["status"],
            "test_type": retry_info["test_type"],
            "instance_type": retry_info["instance_type"],
            "build_mode": retry_info["build_mode"],
            "max_retries": retry_info["max_retries"],
            "attempt": retry_info["attempt"],
            "current_step": retry_info["current_step"],
            "last_error": retry_info.get("last_error"),
            "claude_analysis": retry_info.get("last_analysis"),
            "claude_fixes": retry_info.get("suggested_fixes", []),
            "total_elapsed_minutes": total_elapsed_min,  # Total time since start
            "attempts": [
                {
                    "attempt": a.attempt_number,
                    "error": a.error_message,
                    "timestamp": a.timestamp
                }
                for a in retry_info.get("attempts", [])
            ]
        }
        
        return response
