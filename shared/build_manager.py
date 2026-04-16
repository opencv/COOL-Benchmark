#!/usr/bin/env python3
"""
Build Manager - Handles OpenCV installation and compilation on EC2 instances
"""

import asyncio
import logging
import boto3
import os
from typing import Dict, Any, Optional
import time

logger = logging.getLogger("build-manager")

class BuildManager:
    """Manages OpenCV builds on EC2 instances"""
    
    def __init__(self):
        self.ec2_client = boto3.client('ec2')
        self.ssm_client = boto3.client('ssm')
    
    def _minify_python_code(self, code: str) -> str:
        """
        Minify Python code to reduce size for user-data (16KB AWS limit)
        Removes comments, docstrings, and excessive whitespace while preserving functionality
        """
        import re
        
        lines = code.split('\n')
        minified_lines = []
        in_multiline_string = False
        multiline_delimiter = None
        
        for line in lines:
            stripped = line.strip()
            
            # Skip empty lines
            if not stripped:
                continue
            
            # Handle multiline strings (docstrings)
            if '"""' in stripped or "'''" in stripped:
                if not in_multiline_string:
                    # Starting a multiline string
                    if stripped.startswith('"""') or stripped.startswith("'''"):
                        # This is a docstring, skip it
                        multiline_delimiter = '"""' if '"""' in stripped else "'''"
                        if stripped.count(multiline_delimiter) == 2:
                            # Single-line docstring, skip entirely
                            continue
                        else:
                            in_multiline_string = True
                            continue
                    else:
                        # Multiline string in code (keep it)
                        minified_lines.append(line.rstrip())
                        continue
                else:
                    # Ending a multiline string
                    if multiline_delimiter in stripped:
                        in_multiline_string = False
                        multiline_delimiter = None
                    continue
            
            # Skip lines inside multiline strings (docstrings)
            if in_multiline_string:
                continue
            
            # Skip comment-only lines
            if stripped.startswith('#'):
                continue
            
            # Remove inline comments (but preserve strings with #)
            # Simple approach: remove # and everything after if not in string
            if '#' in line and not ('"""' in line or "'''" in line or '"' in line or "'" in line):
                # Find the # that's not in a string
                in_string = False
                string_char = None
                for i, char in enumerate(line):
                    if char in ('"', "'") and (i == 0 or line[i-1] != '\\'):
                        if not in_string:
                            in_string = True
                            string_char = char
                        elif char == string_char:
                            in_string = False
                            string_char = None
                    elif char == '#' and not in_string:
                        line = line[:i].rstrip()
                        break
            
            # Keep the line if it has content
            if line.strip():
                minified_lines.append(line.rstrip())
        
        # Join lines and reduce multiple blank lines to single
        minified = '\n'.join(minified_lines)
        # Remove multiple consecutive newlines
        minified = re.sub(r'\n\n+', '\n\n', minified)
        
        # Compress and base64 encode
        import gzip
        import base64
        compressed = gzip.compress(minified.encode('utf-8'))
        b64_encoded = base64.b64encode(compressed).decode('ascii')
        
        return b64_encoded
    
    async def _deploy_mcp_server(self, instance_id: str) -> dict:
        """Deploy MCP server to EC2 instance"""
        try:
            logger.info(f"Deploying MCP server to {instance_id}")
            
            # Read the MCP server code with UTF-8 encoding to avoid Windows charset issues
            with open('../opencv-ami/opencv-mcp-server.py', 'r', encoding='utf-8') as f:
                mcp_server_code = f.read()
            
            # Create deployment script
            commands = [
                'mkdir -p /opt/opencv-mcp',
                'cd /opt/opencv-mcp',
                # Write the MCP server code
                f'cat > opencv_mcp_server.py << \'EOFPYTHON\'\n{mcp_server_code}\nEOFPYTHON',
                'chmod +x opencv_mcp_server.py',
                # Start MCP server in background
                'nohup python3 opencv_mcp_server.py > /var/log/opencv-mcp.log 2>&1 &',
                'sleep 5',  # Give server time to start
                # Check if server is running
                'curl -s http://localhost:8080/health || echo "MCP server not responding"'
            ]
            
            result = await self._execute_ssm_command(instance_id, commands, timeout=120)
            
            if result['status'] == 'success':
                logger.info(f"MCP server deployed successfully on {instance_id}")
                return {'status': 'success', 'stdout': result['stdout']}
            else:
                logger.error(f"Failed to deploy MCP server: {result.get('error')}")
                return result
                
        except Exception as e:
            logger.error(f"Error deploying MCP server: {e}")
            return {'status': 'failed', 'error': str(e)}
    
    async def _wait_for_ssm_ready(self, instance_id: str, timeout: int = 300) -> bool:
        """Wait for SSM agent to be ready on instance"""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                response = self.ssm_client.describe_instance_information(
                    Filters=[
                        {'Key': 'InstanceIds', 'Values': [instance_id]}
                    ]
                )
                
                if response['InstanceInformationList']:
                    info = response['InstanceInformationList'][0]
                    if info['PingStatus'] == 'Online':
                        logger.info(f"SSM agent ready on {instance_id}")
                        return True
            except Exception as e:
                logger.debug(f"Waiting for SSM agent: {e}")
            
            await asyncio.sleep(10)
        
        logger.error(f"SSM agent not ready on {instance_id} after {timeout}s")
        return False
    
    async def _execute_ssm_command(self, instance_id: str, commands: list, timeout: int = 600) -> dict:
        """Execute commands via SSM and wait for completion"""
        try:
            # Send command
            response = self.ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName='AWS-RunShellScript',
                Parameters={'commands': commands},
                TimeoutSeconds=timeout
            )
            
            command_id = response['Command']['CommandId']
            logger.info(f"Sent SSM command {command_id} to {instance_id}")
            
            # Wait for command to complete
            start_time = time.time()
            last_log_time = start_time
            while time.time() - start_time < timeout:
                try:
                    result = self.ssm_client.get_command_invocation(
                        CommandId=command_id,
                        InstanceId=instance_id
                    )
                    
                    status = result['Status']
                    
                    # Log progress every 30 seconds
                    if time.time() - last_log_time > 30:
                        elapsed = int(time.time() - start_time)
                        logger.info(f"SSM command {command_id} still running... ({elapsed}s elapsed, status: {status})")
                        last_log_time = time.time()
                    
                    if status == 'Success':
                        logger.info(f"✅ Command {command_id} completed successfully")
                        # Handle encoding issues with SSM output on Windows
                        stdout = result.get('StandardOutputContent', '')
                        stderr = result.get('StandardErrorContent', '')
                        # Replace problematic characters that can't be encoded
                        if isinstance(stdout, str):
                            stdout = stdout.encode('utf-8', errors='replace').decode('utf-8')
                        if isinstance(stderr, str):
                            stderr = stderr.encode('utf-8', errors='replace').decode('utf-8')
                        return {
                            'status': 'success',
                            'stdout': stdout,
                            'stderr': stderr
                        }
                    elif status in ['Failed', 'Cancelled', 'TimedOut', 'Delivery TimedOut']:
                        logger.error(f"❌ Command {command_id} failed: {status}")
                        # Handle encoding issues with SSM output on Windows
                        stdout = result.get('StandardOutputContent', '')
                        stderr = result.get('StandardErrorContent', '')
                        if isinstance(stdout, str):
                            stdout = stdout.encode('utf-8', errors='replace').decode('utf-8')
                        if isinstance(stderr, str):
                            stderr = stderr.encode('utf-8', errors='replace').decode('utf-8')
                        logger.error(f"stdout: {stdout[:500]}")
                        logger.error(f"stderr: {stderr[:500]}")
                        
                        # Create a more informative error message
                        error_msg = status
                        if stderr:
                            # Extract key error lines from stderr (last 5 lines or first error)
                            stderr_lines = stderr.strip().split('\n')
                            key_errors = [line for line in stderr_lines if 'error' in line.lower() or 'failed' in line.lower() or 'no match' in line.lower()]
                            if key_errors:
                                error_msg = f"{status}: {key_errors[0][:200]}"
                            else:
                                # Use last few lines of stderr
                                error_msg = f"{status}: {' | '.join(stderr_lines[-3:])[:200]}"
                        
                        return {
                            'status': 'failed',
                            'stdout': stdout,
                            'stderr': stderr,
                            'error': error_msg
                        }
                    
                    # Still running
                    await asyncio.sleep(5)
                    
                except self.ssm_client.exceptions.InvocationDoesNotExist:
                    # Command not yet available
                    elapsed = int(time.time() - start_time)
                    if elapsed > 30:
                        logger.warning(f"Command {command_id} invocation not found after {elapsed}s")
                    await asyncio.sleep(5)
            
            return {'status': 'timeout', 'error': 'Command execution timed out'}
            
        except Exception as e:
            logger.error(f"Error executing SSM command: {e}")
            return {'status': 'failed', 'error': str(e)}
    
    async def install_opencv_pip(self, instance_id: str, architecture: str = "arm64") -> Dict[str, Any]:
        """
        Install OpenCV via pip (quick mode ~10 minutes)
        Uses user-data script - just waits for completion
        
        Args:
            instance_id: EC2 instance ID
            architecture: 'arm64' for Graviton or 'x86_64' for Intel
            
        Returns:
            Dict with status, duration, and any errors
        """
        start_time = time.time()
        
        try:
            logger.info(f"🚀 OpenCV pip installation started via user-data on {instance_id} ({architecture})")
            logger.info(f"⏳ Waiting for user-data script to complete (checking for completion marker)...")
            
            # Wait for the user-data script to complete (marked by console output)
            max_wait = 600  # 10 minutes (pip install can be slow on some instances)
            check_interval = 5  # Check every 5 seconds for faster error detection
            last_output_length = 0
            
            for i in range(0, max_wait, check_interval):
                await asyncio.sleep(check_interval)
                
                # Check console output for completion marker (run in executor to avoid blocking)
                try:
                    loop = asyncio.get_event_loop()
                    response = await loop.run_in_executor(
                        None,
                        lambda: self.ec2_client.get_console_output(InstanceId=instance_id)
                    )
                    output = response.get('Output', '')
                    
                    # Show progress if output is growing
                    if len(output) > last_output_length:
                        logger.info(f"📝 Installation in progress... ({i}s elapsed, {len(output)} bytes of console output)")
                        last_output_length = len(output)
                    
                    # Check for success markers (check BEFORE error patterns)
                    # 'OpenCV installation complete' is from user-data script
                    # 'Started opencv-mcp.service' is from systemd (means MCP server is running)
                    if 'OpenCV installation complete' in output or 'Started opencv-mcp.service' in output:
                        duration = time.time() - start_time
                        logger.info(f"✅ OpenCV installed successfully on {instance_id} in {duration:.1f}s")
                        logger.info(f"📋 Installation verified from console output")
                        
                        return {
                            "status": "success",
                            "method": "pip",
                            "duration": duration,
                            "steps_completed": [
                                "System update",
                                "Python installation",
                                "OpenCV pip install",
                                "Dependencies installed",
                                "HTTP server deployed"
                            ],
                            "stdout": "Installation completed via user-data script"
                        }
                    
                    # Check MCP server health endpoint early
                    # On Ubuntu 24.04, the MCP server can start in ~45s
                    if i >= 30:  # Start checking after 30 seconds
                        try:
                            instance_info = await loop.run_in_executor(
                                None,
                                lambda: self.ec2_client.describe_instances(InstanceIds=[instance_id])
                            )
                            public_ip = instance_info['Reservations'][0]['Instances'][0].get('PublicIpAddress')
                            
                            if public_ip:
                                import aiohttp
                                try:
                                    async with aiohttp.ClientSession() as session:
                                        async with session.get(f'http://{public_ip}:8080/health', timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                            if resp.status == 200:
                                                data = await resp.json()
                                                if data.get('opencv_available'):
                                                    duration = time.time() - start_time
                                                    logger.info(f"✅ OpenCV installed successfully on {instance_id} in {duration:.1f}s")
                                                    logger.info(f"📋 Installation verified via MCP health endpoint")
                                                    
                                                    return {
                                                        "status": "success",
                                                        "method": "pip",
                                                        "duration": duration,
                                                        "steps_completed": [
                                                            "System update",
                                                            "Python installation",
                                                            "OpenCV pip install",
                                                            "Dependencies installed",
                                                            "HTTP server deployed"
                                                        ],
                                                        "stdout": "Installation completed (verified via health check)"
                                                    }
                                except:
                                    pass  # Health check failed, continue waiting
                        except:
                            pass  # Couldn't get instance info, continue waiting
                    
                    # Check for specific OpenCV/pip failure patterns only
                    # NOTE: Do NOT match broad patterns like 'FAILED' or 'error' —
                    # Ubuntu 24.04 cloud-init boot output contains benign messages like
                    # '[FAILED] Failed to start cloud-final.service' which are NOT errors.
                    import re
                    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
                    output_clean = ansi_escape.sub('', output)
                    
                    failure_patterns = [
                        ('ERROR: Could not find a version', 'Package version not found'),
                        ('ERROR: No matching distribution', 'Package not available'),
                        ('ERROR: Failed building wheel', 'Build compilation failed'),
                        ('ERROR: Command errored out', 'Installation command failed'),
                        ('pip install failed', 'Pip install failed'),
                        ('E: Unable to locate package', 'System package not found'),
                        ('E: Failed to fetch', 'Package download failed'),
                        ('fatal error:', 'Compilation error'),
                        ('No space left on device', 'Disk space exhausted'),
                        ('Connection timed out', 'Network timeout'),
                        ('Could not resolve host', 'DNS resolution failed'),
                        ('Failed to start opencv-mcp', 'MCP service failed to start'),
                        ('Traceback (most recent call last)', 'Python exception occurred')
                    ]
                    
                    for pattern, error_msg in failure_patterns:
                        if pattern in output_clean:
                            duration = time.time() - start_time
                            logger.error(f"❌ Installation failed after {duration:.1f}s: {error_msg}")
                            logger.error(f"Pattern matched: '{pattern}'")
                            logger.error(f"Console output (last 2000 chars):\n{output[-2000:]}")
                            return {
                                "status": "failed",
                                "method": "pip",
                                "duration": duration,
                                "error": f"{error_msg} (detected after {duration:.0f}s)",
                                "stderr": output[-2000:]  # Include more context
                            }
                    
                    logger.debug(f"Waiting for installation... ({i}s elapsed)")
                    
                except Exception as e:
                    logger.debug(f"Console output not yet available: {e}")
            
            # Timeout
            logger.error(f"❌ OpenCV installation timed out after {max_wait}s")
            return {
                "status": "failed",
                "method": "pip",
                "duration": time.time() - start_time,
                "error": f"Installation timed out after {max_wait}s"
            }
            
        except Exception as e:
            logger.error(f"❌ Exception during OpenCV pip installation: {e}")
            return {
                "status": "failed",
                "method": "pip",
                "duration": time.time() - start_time,
                "error": str(e)
            }
    
    async def compile_opencv_from_source(self, instance_id: str, architecture: str = "arm64") -> Dict[str, Any]:
        """
        Compile OpenCV from source with optimizations (slow mode ~30-45 minutes)
        
        Args:
            instance_id: EC2 instance ID
            architecture: 'arm64' for Graviton or 'x86_64' for Intel
            
        Returns:
            Dict with status, duration, build progress, and any errors
        """
        start_time = time.time()
        build_steps = []
        
        try:
            logger.info(f"Compiling OpenCV from source on {instance_id} ({architecture})")
            
            # Wait for SSM agent
            if not await self._wait_for_ssm_ready(instance_id):
                return {
                    "status": "failed",
                    "method": "compile",
                    "duration": time.time() - start_time,
                    "error": "SSM agent not ready"
                }
            
            # Determine optimization flags based on architecture
            if architecture == "arm64":
                opt_flags = "-O3 -mcpu=native -mtune=native"
                cpu_features = "ENABLE_NEON=ON CPU_BASELINE=NEON CPU_DISPATCH=NEON,NEON_FP16"
            else:  # x86_64
                opt_flags = "-O3 -march=native -mtune=native"
                cpu_features = "CPU_BASELINE=SSE4_2 CPU_DISPATCH=AVX,AVX2"
            
            # Compilation commands
            commands = [
                'set -e',
                'echo "Step 1: Updating system..."',
                'sudo apt-get update -y',
                'echo "Step 2: Installing dependencies..."',
                'sudo apt-get install -y build-essential',
                'sudo apt-get install -y cmake python3-dev python3-pip git',
                'pip3 install --break-system-packages numpy aiohttp',
                'echo "Step 3: Downloading OpenCV..."',
                'cd /tmp',
                'wget -q -O opencv.zip https://github.com/opencv/opencv/archive/4.8.1.zip',
                'unzip -q opencv.zip',
                'echo "Step 4: Configuring build..."',
                'cd opencv-4.8.1',
                'mkdir build && cd build',
                f'cmake -D CMAKE_BUILD_TYPE=RELEASE -D CMAKE_C_FLAGS="{opt_flags}" -D CMAKE_CXX_FLAGS="{opt_flags}" -D {cpu_features} -D BUILD_opencv_python3=ON ..',
                'echo "Step 5: Compiling OpenCV (this takes 20-30 minutes)..."',
                'make -j$(nproc)',
                'echo "Step 6: Installing OpenCV..."',
                'sudo make install',
                'sudo ldconfig',
                'python3 -c "import cv2; print(f\\"OpenCV {cv2.__version__} compiled successfully\\")"',
                'echo "Build complete!"'
            ]
            
            # Execute compilation (this will take a long time)
            result = await self._execute_ssm_command(instance_id, commands, timeout=3600)
            
            total_duration = time.time() - start_time
            
            if result['status'] == 'success':
                logger.info(f"OpenCV compiled successfully on {instance_id} in {total_duration:.1f}s")
                
                return {
                    "status": "success",
                    "method": "compile",
                    "duration": total_duration,
                    "steps_completed": [
                        "System update",
                        "Dependencies installed",
                        "OpenCV source downloaded",
                        "Build configured",
                        "OpenCV compiled",
                        "OpenCV installed"
                    ],
                    "optimization_flags": opt_flags,
                    "cpu_features": cpu_features,
                    "stdout": result['stdout']
                }
            else:
                return {
                    "status": "failed",
                    "method": "compile",
                    "duration": total_duration,
                    "error": result.get('error', 'Unknown error'),
                    "stderr": result.get('stderr', '')
                }
            
        except Exception as e:
            logger.error(f"Error compiling OpenCV: {e}")
            return {
                "status": "failed",
                "method": "compile",
                "duration": time.time() - start_time,
                "error": str(e)
            }
    
    async def use_marketplace_ami(self, instance_id: str, license_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Use pre-built marketplace AMI (fastest, ~2 minutes)
        MCP server is deployed via user-data script
        
        Args:
            instance_id: EC2 instance ID with marketplace AMI
            license_key: Optional license key for marketplace software
            
        Returns:
            Dict with status and duration
        """
        start_time = time.time()
        
        try:
            logger.info(f"Using marketplace AMI on {instance_id}")
            logger.info(f"MCP server deployment handled by user-data script")
            
            # Wait for the user-data script to complete
            logger.info(f"Waiting for marketplace AMI initialization...")
            max_wait = 300  # 5 minutes
            check_interval = 5
            last_output_length = 0
            
            for i in range(0, max_wait, check_interval):
                await asyncio.sleep(check_interval)
                
                # Check console output for completion marker
                try:
                    loop = asyncio.get_event_loop()
                    response = await loop.run_in_executor(
                        None,
                        lambda: self.ec2_client.get_console_output(InstanceId=instance_id)
                    )
                    output = response.get('Output', '')
                    
                    # Show progress if output is growing
                    if len(output) > last_output_length:
                        logger.info(f"📝 Initialization in progress... ({i}s elapsed, {len(output)} bytes of console output)")
                        last_output_length = len(output)
                    
                    # Check for success marker
                    if 'Marketplace AMI ready with MCP server' in output:
                        duration = time.time() - start_time
                        logger.info(f"✅ Marketplace AMI ready with MCP server in {duration:.1f}s")
                        
                        return {
                            "status": "success",
                            "method": "marketplace",
                            "duration": duration,
                            "license_configured": license_key is not None,
                            "steps_completed": [
                                "Instance launched",
                                "aiohttp installed in COOL venv",
                                "MCP server deployed",
                                "OpenCV pre-installed (COOL)"
                            ]
                        }
                    
                    # Fallback: Check MCP server health endpoint
                    if i >= 60:  # Start checking after 1 minute
                        try:
                            instance_info = await loop.run_in_executor(
                                None,
                                lambda: self.ec2_client.describe_instances(InstanceIds=[instance_id])
                            )
                            public_ip = instance_info['Reservations'][0]['Instances'][0].get('PublicIpAddress')
                            
                            if public_ip:
                                import aiohttp
                                try:
                                    async with aiohttp.ClientSession() as session:
                                        async with session.get(f'http://{public_ip}:8080/health', timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                            if resp.status == 200:
                                                data = await resp.json()
                                                if data.get('opencv_available'):
                                                    duration = time.time() - start_time
                                                    logger.info(f"✅ Marketplace AMI ready in {duration:.1f}s (verified via health check)")
                                                    
                                                    return {
                                                        "status": "success",
                                                        "method": "marketplace",
                                                        "duration": duration,
                                                        "license_configured": license_key is not None,
                                                        "steps_completed": [
                                                            "Instance launched",
                                                            "MCP server deployed",
                                                            "OpenCV pre-installed (COOL)"
                                                        ]
                                                    }
                                except:
                                    pass
                        except:
                            pass
                    
                except Exception as check_error:
                    logger.debug(f"Console check error (will retry): {check_error}")
            
            # Timeout - fetch detailed logs
            logger.error("❌ Marketplace AMI initialization timed out")
            logger.error("Fetching console output and logs for diagnosis...")
            
            try:
                # Get final console output
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: self.ec2_client.get_console_output(InstanceId=instance_id)
                )
                console_output = response.get('Output', 'No console output available')
                
                logger.error("="*60)
                logger.error("CONSOLE OUTPUT (last 2000 chars):")
                logger.error("="*60)
                logger.error(console_output[-2000:] if len(console_output) > 2000 else console_output)
                
                # Try to get logs via SSM if available
                try:
                    import boto3
                    ssm = boto3.client('ssm', region_name=os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1")))
                    
                    # Check if SSM is connected
                    ssm_response = ssm.describe_instance_information(
                        Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}]
                    )
                    
                    if ssm_response['InstanceInformationList']:
                        logger.error("\n" + "="*60)
                        logger.error("FETCHING SETUP LOGS VIA SSM:")
                        logger.error("="*60)
                        
                        cmd_response = ssm.send_command(
                            InstanceIds=[instance_id],
                            DocumentName='AWS-RunShellScript',
                            Parameters={'commands': [
                                'echo "=== SETUP LOG ==="; tail -100 /var/log/opencv-setup.log 2>&1 || echo "No setup log";',
                                'echo ""; echo "=== MCP SERVICE STATUS ==="; systemctl status opencv-mcp --no-pager 2>&1 || echo "Service not found";',
                                'echo ""; echo "=== MCP LOG ==="; tail -50 /var/log/opencv-mcp.log 2>&1 || echo "No MCP log";'
                            ]},
                            TimeoutSeconds=30
                        )
                        
                        command_id = cmd_response['Command']['CommandId']
                        
                        # Wait for command
                        for _ in range(10):
                            await asyncio.sleep(1)
                            result = ssm.get_command_invocation(
                                CommandId=command_id,
                                InstanceId=instance_id
                            )
                            if result['Status'] in ['Success', 'Failed']:
                                logger.error(result.get('StandardOutputContent', ''))
                                break
                    else:
                        logger.error("SSM not connected - cannot fetch detailed logs")
                        
                except Exception as ssm_error:
                    logger.error(f"Could not fetch SSM logs: {ssm_error}")
                    
            except Exception as log_error:
                logger.error(f"Could not fetch diagnostic logs: {log_error}")
            
            return {
                "status": "failed",
                "method": "marketplace",
                "duration": time.time() - start_time,
                "error": f"Marketplace AMI initialization timed out after 5 minutes. Console output (last 2000 chars): {console_output[-2000:] if 'console_output' in locals() else 'unavailable'}"
            }
            
        except Exception as e:
            logger.error(f"Error with marketplace AMI: {e}")
            return {
                "status": "failed",
                "method": "marketplace",
                "duration": time.time() - start_time,
                "error": str(e)
            }
            
        except Exception as e:
            logger.error(f"Error with marketplace AMI: {e}")
            return {
                "status": "failed",
                "method": "marketplace",
                "duration": time.time() - start_time,
                "error": str(e)
            }
    
    async def _deploy_mcp_server_via_ssm(self, instance_id: str) -> dict:
        """Deploy MCP server to instance using SSM"""
        try:
            logger.info(f"Deploying MCP server to {instance_id} via SSM")
            
            # Read the MCP server code with UTF-8 encoding
            with open('../opencv-ami/opencv-mcp-server.py', 'r', encoding='utf-8') as f:
                mcp_server_code = f.read()
            
            # Create deployment commands - use COOL optimized OpenCV from marketplace AMI
            commands = [
                'echo "Installing Python dependencies for MCP server..."',
                # Install aiohttp into COOL venv (OpenCV is already in /opt/cool)
                'sudo /opt/cool/venvs/python_3.12/bin/pip install aiohttp 2>&1 || echo "pip install failed"',
                '/opt/cool/venvs/python_3.12/bin/python -c "import aiohttp; print(\'aiohttp installed\')" 2>&1',
                'echo "Creating MCP server directory..."',
                'sudo mkdir -p /opt/opencv-mcp',
                'echo "Writing MCP server code..."',
                f'sudo tee /opt/opencv-mcp/opencv_mcp_server.py > /dev/null << \'EOFPYTHON\'\n{mcp_server_code}\nEOFPYTHON',
                'sudo chmod +x /opt/opencv-mcp/opencv_mcp_server.py',
                'echo "MCP server file created"',
                'echo "Creating systemd service with COOL OpenCV paths..."',
                '''cat > /tmp/opencv-mcp.service << 'EOFSYSTEMD'
[Unit]
Description=OpenCV MCP Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/opencv-mcp
Environment="PYTHONUNBUFFERED=1"
ExecStart=/opt/cool/venvs/python_3.12/bin/python /opt/opencv-mcp/opencv_mcp_server.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/opencv-mcp.log
StandardError=append:/var/log/opencv-mcp.log

[Install]
WantedBy=multi-user.target
EOFSYSTEMD''',
                'sudo mv /tmp/opencv-mcp.service /etc/systemd/system/',
                'sudo systemctl daemon-reload',
                'sudo systemctl enable opencv-mcp',
                'sudo systemctl start opencv-mcp',
                'sleep 5',
                'echo "Checking service status..."',
                'sudo systemctl status opencv-mcp --no-pager || true',
                'echo "Checking if MCP server is responding..."',
                'curl -s http://localhost:8080/health || echo "MCP server not responding yet"',
                'echo "MCP server deployment complete"'
            ]
            
            result = await self._execute_ssm_command(instance_id, commands, timeout=300)
            
            if result['status'] == 'success':
                logger.info(f"✅ MCP server deployed successfully via SSM on {instance_id}")
                return {'status': 'success', 'stdout': result['stdout']}
            else:
                logger.error(f"❌ Failed to deploy MCP server via SSM: {result.get('error')}")
                return result
                
        except Exception as e:
            logger.error(f"Error deploying MCP server via SSM: {e}")
            return {'status': 'failed', 'error': str(e)}
    
    async def get_build_progress(self, instance_id: str) -> Dict[str, Any]:
        """
        Get current build progress from an instance
        
        Args:
            instance_id: EC2 instance ID
            
        Returns:
            Dict with current step and progress percentage
        """
        try:
            # In production, use SSM to read /tmp/build-progress.txt
            # For demo, return mock progress
            
            return {
                "current_step": "Compiling OpenCV",
                "progress": 65,
                "estimated_remaining": 600  # seconds
            }
            
        except Exception as e:
            logger.error(f"Error getting build progress: {e}")
            return {
                "current_step": "Unknown",
                "progress": 0,
                "error": str(e)
            }
    
    def get_user_data_script(self, build_mode: str, architecture: str) -> str:
        """
        Generate user data script based on build mode
        
        Args:
            build_mode: 'pip', 'compile', or 'marketplace'
            architecture: 'arm64' or 'x86_64'
            
        Returns:
            User data script as string
        """
        if build_mode == "pip":
            user_data = self._get_pip_user_data()
        elif build_mode == "compile":
            user_data = self._get_compile_user_data(architecture)
        else:  # marketplace
            user_data = self._get_marketplace_user_data()
        
        # Check size (AWS limit is 16384 bytes)
        user_data_bytes = len(user_data.encode('utf-8'))
        if user_data_bytes > 16384:
            logger.error(f"User-data script is too large: {user_data_bytes} bytes (limit: 16384)")
            raise ValueError(f"User-data script exceeds AWS limit: {user_data_bytes} bytes > 16384 bytes")
        elif user_data_bytes > 14000:
            logger.warning(f"User-data script is close to AWS limit: {user_data_bytes} bytes / 16384 bytes")
        else:
            logger.info(f"User-data script size: {user_data_bytes} bytes / 16384 bytes")
        
        return user_data
    
    def _get_pip_user_data(self) -> str:
        """Get user data script for pip installation"""
        # Read the MCP server code - use absolute path relative to this file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        mcp_server_path = os.path.join(current_dir, '..', 'opencv-ami', 'opencv-mcp-server.py')
        mcp_server_path = os.path.abspath(mcp_server_path)
        
        try:
            with open(mcp_server_path, 'r', encoding='utf-8') as f:
                mcp_server_code = f.read()
                # Minify the code to reduce size (remove comments, extra whitespace)
                mcp_server_code = self._minify_python_code(mcp_server_code)
                logger.info(f"Successfully read MCP server code from {mcp_server_path} ({len(mcp_server_code)} bytes after minification)")
        except Exception as e:
            logger.error(f"Failed to read MCP server code from {mcp_server_path}: {e}")
            # Try alternative path (in case running from different directory)
            alt_path = os.path.join(os.getcwd(), 'opencv-ami', 'opencv-mcp-server.py')
            try:
                with open(alt_path, 'r', encoding='utf-8') as f:
                    mcp_server_code = f.read()
                    mcp_server_code = self._minify_python_code(mcp_server_code)
                    logger.info(f"Successfully read MCP server code from alternative path {alt_path}")
            except Exception as e2:
                logger.error(f"Failed to read MCP server code from {alt_path}: {e2}")
                mcp_server_code = "# MCP server code not found"
        
        return f"""#!/bin/bash
echo "=== USER DATA SCRIPT STARTING ==="
date
echo "Current user: $(whoami)"
echo "Current directory: $(pwd)"

echo "Step 1: Updating system packages..."
apt-get update -y
echo "Step 1 complete"

echo "Step 2: Installing Python3 and dependencies..."
apt-get install -y python3 python3-pip python3-venv libxext-dev libsm-dev libxrender-dev libxcb1-dev
echo "Step 2 complete"

echo "Step 3: Installing OpenCV via pip..."

# Remove numpy RECORD file to allow pip to manage it
rm -f /usr/lib/python3/dist-packages/numpy-*.dist-info/RECORD 2>/dev/null || true

# Install with --break-system-packages (REQUIRED on Ubuntu 24.04)
if ! pip3 install --break-system-packages --ignore-installed numpy opencv-python-headless==4.12.0.88 Pillow scipy aiohttp; then
    echo "ERROR: pip install failed with exit code $?"
    echo "Checking what went wrong..."
    pip3 list | grep -E "(opencv|aiohttp|numpy)" || echo "No packages found"
    df -h
    exit 1
fi

echo "Step 3 complete"


echo "Step 4: Verifying OpenCV..."
python3 -c "import cv2; print(f'OpenCV {{cv2.__version__}} installed')"
python3 -c "import aiohttp; print(f'aiohttp {{aiohttp.__version__}} installed')"
echo "Step 4 complete"

echo "Step 5: Deploying MCP server..."
mkdir -p /opt/opencv-mcp
cd /opt/opencv-mcp

echo "{mcp_server_code}" | base64 -d | gunzip > opencv_mcp_server.py

chmod +x opencv_mcp_server.py
echo "Step 5 complete"

echo "Step 6: Creating systemd service..."
cat > /etc/systemd/system/opencv-mcp.service << 'EOFSYSTEMD'
[Unit]
Description=OpenCV MCP Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/opencv-mcp
ExecStart=/usr/bin/python3 /opt/opencv-mcp/opencv_mcp_server.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/opencv-mcp.log
StandardError=append:/var/log/opencv-mcp.log
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
EOFSYSTEMD

systemctl daemon-reload
systemctl enable opencv-mcp
systemctl start opencv-mcp

# Ensure local OS firewall isn't blocking port 8080
echo "Configuring OS firewall rules..."
iptables -I INPUT -p tcp --dport 8080 -j ACCEPT || true
ufw allow 8080/tcp || true

echo "Step 6 complete"

echo "Step 7: Waiting for MCP server..."
sleep 5

if ! systemctl is-active --quiet opencv-mcp; then
    echo "ERROR: MCP service failed to start"
    systemctl status opencv-mcp
    journalctl -u opencv-mcp -n 50
    exit 1
fi

for i in $(seq 1 60); do
    if curl -s http://localhost:8080/health > /dev/null 2>&1; then
        echo "MCP server is healthy"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "ERROR: MCP server not responding after 60 seconds"
        systemctl status opencv-mcp
        journalctl -u opencv-mcp -n 50
        exit 1
    fi
    sleep 1
done
echo "Step 7 complete"

echo "Step 8: Creating ready marker..."
touch /tmp/opencv-ready
echo "=== OpenCV installation complete ==="
date
exit 0
"""
    
    def _get_compile_user_data(self, architecture: str) -> str:
        """Get user data script for compilation"""
        if architecture == "arm64":
            opt_flags = "-O3 -mcpu=native -mtune=native"
            cpu_features = "ENABLE_NEON=ON CPU_BASELINE=NEON"
        else:
            opt_flags = "-O3 -march=native -mtune=native"
            cpu_features = "CPU_BASELINE=SSE4_2"
        
        return f"""#!/bin/bash
set -e
apt-get update -y
apt-get install -y build-essential
apt-get install -y cmake python3-dev python3-pip git
pip3 install --break-system-packages numpy
cd /tmp
wget -O opencv.zip https://github.com/opencv/opencv/archive/4.8.1.zip
unzip opencv.zip
cd opencv-4.8.1
mkdir build && cd build
cmake -D CMAKE_BUILD_TYPE=RELEASE -D CMAKE_C_FLAGS="{opt_flags}" -D CMAKE_CXX_FLAGS="{opt_flags}" -D {cpu_features} ..
make -j$(nproc)
make install
ldconfig
touch /tmp/opencv-ready
"""
    
    def _get_marketplace_user_data(self, license_key: Optional[str] = None) -> str:
        """Get user data script for marketplace AMI - deploys MCP server with COOL venv"""
        
        # Read the MCP server code - use absolute path relative to this file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # Go up from shared/ to GravitonBenchMark/, then into opencv-ami/
        mcp_server_path = os.path.join(current_dir, '..', 'opencv-ami', 'opencv-mcp-server.py')
        mcp_server_path = os.path.abspath(mcp_server_path)  # Resolve to absolute path
        
        try:
            with open(mcp_server_path, 'r', encoding='utf-8') as f:
                mcp_server_code = f.read()
                # Minify the code to reduce size
                mcp_server_code = self._minify_python_code(mcp_server_code)
                logger.info(f"Successfully read MCP server code from {mcp_server_path} ({len(mcp_server_code)} bytes after minification)")
        except Exception as e:
            logger.error(f"Failed to read MCP server code from {mcp_server_path}: {e}")
            # Try alternative path (in case running from different directory)
            alt_path = os.path.join(os.getcwd(), 'opencv-ami', 'opencv-mcp-server.py')
            try:
                with open(alt_path, 'r', encoding='utf-8') as f:
                    mcp_server_code = f.read()
                    mcp_server_code = self._minify_python_code(mcp_server_code)
                    logger.info(f"Successfully read MCP server code from alternative path {alt_path}")
            except Exception as e2:
                logger.error(f"Failed to read MCP server code from {alt_path}: {e2}")
                mcp_server_code = "# MCP server code not found"
        
        license_config = ""
        if license_key:
            license_config = f"""
# Configure marketplace license
export OPENCV_LICENSE_KEY="{license_key}"
echo "export OPENCV_LICENSE_KEY={license_key}" >> /etc/environment
"""
        
        return f"""#!/bin/bash
echo "=== MARKETPLACE AMI INITIALIZATION ==="
date

{license_config}

# Install SSM agent if not present
if ! command -v amazon-ssm-agent &> /dev/null; then
    echo "Installing SSM agent..."
    apt-get install -y amazon-ssm-agent || snap install amazon-ssm-agent --classic
    systemctl enable amazon-ssm-agent
    systemctl start amazon-ssm-agent
    echo "SSM agent installed and started"
else
    echo "SSM agent already installed"
    systemctl restart amazon-ssm-agent
fi

echo "Step 1: Installing aiohttp into COOL venv..."
/opt/cool/venvs/python_3.12/bin/pip install aiohttp Pillow numpy
echo "Step 1 complete"

echo "Step 2: Verifying COOL OpenCV..."
/opt/cool/venvs/python_3.12/bin/python -c "import cv2; print(f'OpenCV {{cv2.__version__}} installed')"
/opt/cool/venvs/python_3.12/bin/python -c "import aiohttp; print(f'aiohttp {{aiohttp.__version__}} installed')"
echo "Step 2 complete"

echo "Step 3: Deploying MCP server..."
mkdir -p /opt/opencv-mcp
cd /opt/opencv-mcp

echo "{mcp_server_code}" | base64 -d | gunzip > opencv_mcp_server.py

chmod +x opencv_mcp_server.py
echo "Step 3 complete"

echo "Step 4: Creating systemd service with COOL venv Python..."
cat > /etc/systemd/system/opencv-mcp.service << 'EOFSYSTEMD'
[Unit]
Description=OpenCV MCP Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/opencv-mcp
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONPATH=/opt/cool/python_3.12/site-packages:/opt/cool/python_3.11/site-packages"
Environment="LD_LIBRARY_PATH=/opt/cool/cpp_sdk/lib:/opt/cool/lib"
ExecStart=/opt/cool/venvs/python_3.12/bin/python /opt/opencv-mcp/opencv_mcp_server.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/opencv-mcp.log
StandardError=append:/var/log/opencv-mcp.log

[Install]
WantedBy=multi-user.target
EOFSYSTEMD

systemctl daemon-reload
systemctl enable opencv-mcp
systemctl start opencv-mcp

# Ensure local OS firewall isn't blocking port 8080
echo "Configuring OS firewall rules..."
iptables -I INPUT -p tcp --dport 8080 -j ACCEPT || true
ufw allow 8080/tcp || true

echo "Step 4 complete"

echo "Step 5: Waiting for MCP server..."
sleep 5

# Check if service is running
if ! systemctl is-active --quiet opencv-mcp; then
    echo "ERROR: MCP service failed to start"
    systemctl status opencv-mcp
    journalctl -u opencv-mcp -n 50
    exit 1
fi

# Test MCP server health endpoint
for i in {{1..30}}; do
    if curl -s http://localhost:8080/health > /dev/null; then
        echo "MCP server is responding!"
        break
    fi
    echo "Waiting for MCP server... ($i/30)"
    sleep 2
done

echo "Step 5 complete"

echo "=== Marketplace AMI ready with MCP server ==="
date
touch /tmp/opencv-ready
exit 0
"""
