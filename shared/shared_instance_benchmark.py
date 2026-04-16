#!/usr/bin/env python3
"""
Shared Instance Benchmark Utilities

Provides MCP server reconfiguration helpers that allow a single EC2 instance
(launched with the COOL marketplace AMI) to switch between COOL and DIY
(pip-installed) OpenCV builds without needing a new instance.

Used by benchmark_executor.py when reusing an instance whose current
build_mode differs from the requested one.
"""

import asyncio
import logging

logger = logging.getLogger("shared-instance-benchmark")


async def reconfigure_mcp_for_diy(instance_id: str, build_manager):
    """
    Reconfigure the MCP server on a COOL marketplace instance to use
    pip-installed (DIY) OpenCV instead.

    Steps:
      1. Stop the current COOL MCP systemd service
      2. Install opencv-python-headless via pip (system Python)
      3. Overwrite the systemd unit to use /usr/bin/python3
      4. Restart MCP service
    """
    logger.info(f"🔧 Reconfiguring {instance_id}: COOL → DIY (pip)")

    commands = [
        # Step 1: Stop the COOL MCP server
        'echo "Step 1: Stopping COOL MCP server..."',
        'sudo systemctl stop opencv-mcp || true',
        'sleep 2',

        # Step 2: Install pip OpenCV under system Python
        'echo "Step 2: Installing OpenCV via pip (system Python)..."',
        'sudo apt-get update -y -qq',
        'sudo apt-get install -y -qq python3-pip || exit 1',
        (
            'pip3 install --break-system-packages '
            'opencv-python-headless==4.12.0.88 numpy Pillow scipy aiohttp || exit 1'
        ),
        'python3 -c "import cv2; print(f\'DIY OpenCV {cv2.__version__} installed\')" || exit 1',

        # Step 3: Overwrite systemd unit to use system Python
        'echo "Step 3: Reconfiguring systemd service for DIY..."',
        '''cat > /tmp/opencv-mcp-diy.service << 'EOFSYSTEMD'
[Unit]
Description=OpenCV MCP Server (DIY pip)
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
EOFSYSTEMD''',
        'sudo mv /tmp/opencv-mcp-diy.service /etc/systemd/system/opencv-mcp.service',
        'sudo systemctl daemon-reload || exit 1',

        # Step 4: Restart with DIY config
        'echo "Step 4: Starting DIY MCP server..."',
        'sudo systemctl start opencv-mcp',
        'sleep 5',

        # Step 5: Wait for service to become active (no set -e, so loop works)
        'echo "Step 5: Verifying DIY MCP server..."',
        'for i in $(seq 1 30); do STATUS=$(systemctl is-active opencv-mcp 2>/dev/null || true); if [ "$STATUS" = "active" ]; then echo "MCP service active"; break; fi; echo "Waiting for MCP service... ($STATUS, ${i}s)"; sleep 1; done',
        'systemctl is-active opencv-mcp || (echo "ERROR: MCP service not active"; systemctl status opencv-mcp --no-pager; exit 1)',
        'curl -s http://localhost:8080/health || echo "MCP not yet responding"',
        'echo "DIY reconfiguration complete"',
    ]

    result = await build_manager._execute_ssm_command(
        instance_id, commands, timeout=600
    )

    if result['status'] != 'success':
        raise Exception(
            f"DIY reconfiguration failed on {instance_id}: "
            f"{result.get('error', 'Unknown error')}"
        )

    logger.info(f"✅ Instance {instance_id} reconfigured for DIY (pip OpenCV)")
    return result


async def reconfigure_mcp_for_cool(instance_id: str, build_manager):
    """
    Reconfigure the MCP server on an instance to use the COOL optimized
    OpenCV build at /opt/cool/ instead of the pip-installed version.

    Steps:
      1. Stop the current DIY MCP systemd service
      2. Overwrite the systemd unit to use COOL Python + library paths
      3. Restart MCP service
    """
    logger.info(f"🔧 Reconfiguring {instance_id}: DIY → COOL (marketplace)")

    commands = [
        # Step 1: Stop the DIY MCP server
        'echo "Step 1: Stopping DIY MCP server..."',
        'sudo systemctl stop opencv-mcp || true',
        'sleep 2',

        # Step 2: Ensure required packages are installed in COOL venv (MCP server needs them)
        'echo "Step 2: Installing requirements in COOL venv..."',
        'sudo /opt/cool/venvs/python_3.12/bin/pip install aiohttp Pillow numpy 2>&1 || exit 1',
        '/opt/cool/venvs/python_3.12/bin/python -c "import aiohttp; print(\'aiohttp installed in COOL venv\')" || exit 1',
        'export PYTHONPATH=/opt/cool/python_3.12/site-packages:/opt/cool/python_3.11/site-packages',
        'export LD_LIBRARY_PATH=/opt/cool/cpp_sdk/lib:/opt/cool/lib',
        '/opt/cool/venvs/python_3.12/bin/python -c "import cv2; print(f\'COOL OpenCV {cv2.__version__} available\')" || exit 1',

        # Step 3: Overwrite systemd unit to use COOL Python paths
        'echo "Step 3: Reconfiguring systemd service for COOL..."',
        '''cat > /tmp/opencv-mcp-cool.service << 'EOFSYSTEMD'
[Unit]
Description=OpenCV MCP Server (COOL optimized)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/opencv-mcp
ExecStart=/opt/cool/venvs/python_3.12/bin/python /opt/opencv-mcp/opencv_mcp_server.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/opencv-mcp.log
StandardError=append:/var/log/opencv-mcp.log
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONPATH=/opt/cool/python_3.12/site-packages:/opt/cool/python_3.11/site-packages"
Environment="LD_LIBRARY_PATH=/opt/cool/cpp_sdk/lib:/opt/cool/lib"

[Install]
WantedBy=multi-user.target
EOFSYSTEMD''',
        'sudo mv /tmp/opencv-mcp-cool.service /etc/systemd/system/opencv-mcp.service',
        'sudo systemctl daemon-reload || exit 1',

        # Step 4: Restart with COOL config
        'echo "Step 4: Starting COOL MCP server..."',
        'sudo systemctl start opencv-mcp',
        'sleep 5',

        # Step 5: Wait for service to become active (no set -e, so loop works)
        'echo "Step 5: Verifying COOL MCP server..."',
        'for i in $(seq 1 30); do STATUS=$(systemctl is-active opencv-mcp 2>/dev/null || true); if [ "$STATUS" = "active" ]; then echo "MCP service active"; break; fi; echo "Waiting for MCP service... ($STATUS, ${i}s)"; sleep 1; done',
        'systemctl is-active opencv-mcp || (echo "ERROR: MCP service not active"; systemctl status opencv-mcp --no-pager; exit 1)',
        'curl -s http://localhost:8080/health || echo "MCP not yet responding"',
        'echo "COOL reconfiguration complete"',
    ]

    result = await build_manager._execute_ssm_command(
        instance_id, commands, timeout=300
    )

    if result['status'] != 'success':
        raise Exception(
            f"COOL reconfiguration failed on {instance_id}: "
            f"{result.get('error', 'Unknown error')}"
        )

    logger.info(f"✅ Instance {instance_id} reconfigured for COOL (optimized OpenCV)")
    return result
