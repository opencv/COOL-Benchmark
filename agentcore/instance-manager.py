#!/usr/bin/env python3
"""
AgentCore Instance Manager - Manages EC2 instances for OpenCV benchmarking
Handles auto-scaling, load balancing, and cost tracking
"""

import asyncio
import json
import logging
import os
import time
from typing import Dict, List, Optional, Any
import boto3
from dataclasses import dataclass
from enum import Enum
import aiohttp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("instance-manager")

class InstanceState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    TERMINATED = "terminated"

@dataclass
class BenchmarkInstance:
    instance_id: str
    instance_type: str
    state: InstanceState
    private_ip: str
    public_ip: Optional[str]
    launch_time: float
    cost_per_hour: float
    current_load: int = 0
    max_load: int = 10
    build_mode: str = "unknown"  # Track build mode (pip, compile, marketplace)

class InstanceManager:
    def __init__(self, region: str = None):
        region = region or os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
        self.region = region
        self.ec2_client = boto3.client('ec2', region_name=region)
        self.ec2_resource = boto3.resource('ec2', region_name=region)
        self.autoscaling_client = boto3.client('autoscaling', region_name=region)
        
        self.instances: Dict[str, BenchmarkInstance] = {}
        self.load_balancer_queue = asyncio.Queue()
        self.session = aiohttp.ClientSession()
        
        # Instance reuse tracking
        self.instance_pool: Dict[str, Dict[str, Any]] = {}  # instance_id -> {config, last_used, instance_type, build_mode}
        self.idle_timeout_seconds = 7200  # 2 hours
        
        # Configuration
        self.vpc_id = None
        self.subnet_ids = []
        self.security_group_id = None
        self.key_pair_name = os.environ.get("EC2_KEY_PAIR_NAME", "")
        
    async def initialize(self):
        """Initialize the instance manager"""
        try:
            await self._discover_infrastructure()
            # Cleanup any orphaned instances from previous runs
            await self._cleanup_orphaned_instances()
            # Start background task to cleanup idle instances
            asyncio.create_task(self._cleanup_idle_instances_loop())
            logger.info("Instance manager initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize instance manager: {e}")
            raise
    
    async def _discover_infrastructure(self):
        """Discover existing VPC, subnets, and security groups"""
        try:
            # Find VPC
            vpcs = self.ec2_client.describe_vpcs(
                Filters=[{'Name': 'tag:Name', 'Values': ['BenchmarkVPC']}]
            )
            if vpcs['Vpcs']:
                self.vpc_id = vpcs['Vpcs'][0]['VpcId']
                logger.info(f"Found VPC: {self.vpc_id}")
            else:
                # Fall back to default VPC
                logger.warning("BenchmarkVPC not found, using default VPC")
                default_vpcs = self.ec2_client.describe_vpcs(
                    Filters=[{'Name': 'is-default', 'Values': ['true']}]
                )
                if default_vpcs['Vpcs']:
                    self.vpc_id = default_vpcs['Vpcs'][0]['VpcId']
                    logger.info(f"Using default VPC: {self.vpc_id}")
            
            # Find subnets
            if self.vpc_id:
                subnets = self.ec2_client.describe_subnets(
                    Filters=[
                        {'Name': 'vpc-id', 'Values': [self.vpc_id]},
                        {'Name': 'tag:Name', 'Values': ['*Private*']}
                    ]
                )
                self.subnet_ids = [subnet['SubnetId'] for subnet in subnets['Subnets']]
                
                # If no private subnets found, use any subnet in the VPC
                if not self.subnet_ids:
                    logger.warning("No private subnets found, using any available subnet")
                    all_subnets = self.ec2_client.describe_subnets(
                        Filters=[{'Name': 'vpc-id', 'Values': [self.vpc_id]}]
                    )
                    self.subnet_ids = [subnet['SubnetId'] for subnet in all_subnets['Subnets']]
                
                logger.info(f"Found subnets: {self.subnet_ids}")
            
            # Find or create security group with port 8080 open
            if self.vpc_id:
                self.security_group_id = await self._ensure_security_group()
                
        except Exception as e:
            logger.error(f"Error discovering infrastructure: {e}")
    
    async def _ensure_security_group(self):
        """Ensure security group exists with port 8080 open"""
        try:
            # Try to find existing OpenCVSecurityGroup
            sgs = self.ec2_client.describe_security_groups(
                Filters=[
                    {'Name': 'group-name', 'Values': ['OpenCVSecurityGroup']},
                    {'Name': 'vpc-id', 'Values': [self.vpc_id]}
                ]
            )
            
            if sgs['SecurityGroups']:
                sg_id = sgs['SecurityGroups'][0]['GroupId']
                logger.info(f"Found security group: {sg_id}")
                
                # Ensure port 8080 is open
                await self._ensure_port_8080_open(sg_id)
                return sg_id
            else:
                # Create new security group
                logger.info("Creating OpenCVSecurityGroup with port 8080 open...")
                response = self.ec2_client.create_security_group(
                    GroupName='OpenCVSecurityGroup',
                    Description='Security group for OpenCV Benchmark instances - allows MCP server access on port 8080',
                    VpcId=self.vpc_id
                )
                sg_id = response['GroupId']
                
                # Tag the security group
                self.ec2_client.create_tags(
                    Resources=[sg_id],
                    Tags=[
                        {'Key': 'Name', 'Value': 'OpenCVSecurityGroup'},
                        {'Key': 'Project', 'Value': 'OpenCV-Graviton-Benchmark'}
                    ]
                )
                
                # Add inbound rule for port 8080 (from anywhere for now)
                self.ec2_client.authorize_security_group_ingress(
                    GroupId=sg_id,
                    IpPermissions=[
                        {
                            'IpProtocol': 'tcp',
                            'FromPort': 8080,
                            'ToPort': 8080,
                            'IpRanges': [{'CidrIp': '0.0.0.0/0', 'Description': 'MCP server access'}]
                        }
                    ]
                )
                
                logger.info(f"✅ Created security group {sg_id} with port 8080 open")
                return sg_id
                
        except Exception as e:
            logger.error(f"Error ensuring security group: {e}")
            # Fall back to default security group
            logger.warning("Falling back to default security group")
            default_sgs = self.ec2_client.describe_security_groups(
                Filters=[
                    {'Name': 'group-name', 'Values': ['default']},
                    {'Name': 'vpc-id', 'Values': [self.vpc_id]}
                ]
            )
            if default_sgs['SecurityGroups']:
                sg_id = default_sgs['SecurityGroups'][0]['GroupId']
                logger.info(f"Using default security group: {sg_id}")
                # Try to open port 8080 on default group
                await self._ensure_port_8080_open(sg_id)
                return sg_id
            return None
    
    async def _ensure_port_8080_open(self, sg_id):
        """Ensure port 8080 is open in the security group"""
        try:
            # Check if port 8080 is already open
            sg = self.ec2_client.describe_security_groups(GroupIds=[sg_id])
            
            port_8080_open = False
            for permission in sg['SecurityGroups'][0].get('IpPermissions', []):
                if (permission.get('IpProtocol') == 'tcp' and 
                    permission.get('FromPort') == 8080 and 
                    permission.get('ToPort') == 8080):
                    port_8080_open = True
                    break
            
            if not port_8080_open:
                logger.info(f"Opening port 8080 on security group {sg_id}...")
                self.ec2_client.authorize_security_group_ingress(
                    GroupId=sg_id,
                    IpPermissions=[
                        {
                            'IpProtocol': 'tcp',
                            'FromPort': 8080,
                            'ToPort': 8080,
                            'IpRanges': [{'CidrIp': '0.0.0.0/0', 'Description': 'MCP server access'}]
                        }
                    ]
                )
                logger.info(f"✅ Port 8080 opened on security group {sg_id}")
            else:
                logger.info(f"✅ Port 8080 already open on security group {sg_id}")
                
        except self.ec2_client.exceptions.ClientError as e:
            if 'InvalidPermission.Duplicate' in str(e):
                logger.info(f"Port 8080 already open on security group {sg_id}")
            else:
                logger.warning(f"Could not open port 8080 on security group {sg_id}: {e}")
    
    async def launch_instance(
        self, 
        instance_type: str, 
        ami_id: str, 
        user_data: str = None,
        tags: Dict[str, str] = None,
        optimization_mode: str = "optimized"
    ) -> str:
        """Launch a new EC2 instance"""
        try:
            # Use provided user_data or generate default
            if user_data is None:
                user_data = f"""#!/bin/bash
apt-get update -y
apt-get install -y python3 python3-pip

# Install MCP server dependencies
pip3 install --break-system-packages mcp aiohttp boto3 psutil Pillow opencv-python==4.12.0.88 numpy

# Configure optimization mode
echo "Optimization mode: {optimization_mode}" > /tmp/opencv_mode.txt
"""

            # Prepare tags
            current_timestamp = str(int(time.time()))
            termination_timestamp = str(int(time.time() + 7200))  # 2 hours from now
            max_lifetime_hours = 3  # Maximum 3 hours lifetime as safety net
            
            tag_dict = {
                'Name': f'opencv-benchmark-{instance_type}',
                'Project': 'OpenCV-Graviton-Benchmark',
                'OptimizationMode': optimization_mode,
                'LaunchTimestamp': current_timestamp,
                'TerminationTimestamp': termination_timestamp,  # When to auto-terminate
                'MaxLifetimeHours': str(max_lifetime_hours),
                'AutoTerminate': 'true'  # Flag for cleanup scripts
            }
            
            # Merge with provided tags (provided tags override defaults)
            if tags:
                tag_dict.update(tags)
            
            # Convert to list format for AWS API
            tag_list = [{'Key': k, 'Value': v} for k, v in tag_dict.items()]

            # Launch instance
            response = self.ec2_client.run_instances(
                ImageId=ami_id,
                MinCount=1,
                MaxCount=1,
                InstanceType=instance_type,
                KeyName=self.key_pair_name,
                SecurityGroupIds=[self.security_group_id] if self.security_group_id else [],
                SubnetId=self.subnet_ids[0] if self.subnet_ids else None,
                UserData=user_data,
                TagSpecifications=[
                    {
                        'ResourceType': 'instance',
                        'Tags': tag_list
                    }
                ],
                IamInstanceProfile={'Name': 'OpenCVInstanceRole'} if self._check_instance_profile() else {}
            )
            
            instance_id = response['Instances'][0]['InstanceId']
            
            # Get pricing
            cost_per_hour = await self._get_instance_pricing(instance_type)
            
            # Extract build_mode from tags if provided
            build_mode = tags.get('BuildMode', 'unknown') if tags else 'unknown'
            
            # Create instance record
            instance = BenchmarkInstance(
                instance_id=instance_id,
                instance_type=instance_type,
                state=InstanceState.PENDING,
                private_ip="",
                public_ip=None,
                launch_time=time.time(),
                cost_per_hour=cost_per_hour,
                build_mode=build_mode
            )
            
            self.instances[instance_id] = instance
            
            logger.info(f"Launched instance {instance_id} ({instance_type}, build_mode={build_mode})")
            return instance_id
            
        except Exception as e:
            logger.error(f"Error launching instance: {e}")
            raise
    
    def _check_instance_profile(self) -> bool:
        """Check if IAM instance profile exists"""
        try:
            iam = boto3.client('iam')
            iam.get_instance_profile(InstanceProfileName='OpenCVInstanceRole')
            return True
        except:
            return False
    
    async def wait_for_instance_ready(self, instance_id: str, timeout: int = 300) -> bool:
        """Wait for instance to be ready and MCP server to start"""
        start_time = time.time()
        
        # Give AWS a moment to register the instance
        await asyncio.sleep(5)
        
        while time.time() - start_time < timeout:
            try:
                # Check instance state
                response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
                
                if not response['Reservations']:
                    logger.warning(f"Instance {instance_id} not found in reservations yet, waiting...")
                    await asyncio.sleep(10)
                    continue
                
                instance_data = response['Reservations'][0]['Instances'][0]
                
                state = instance_data['State']['Name']
                logger.info(f"Instance {instance_id} state: {state}")
                
                if state == 'running':
                        
                    # Update instance info
                    if instance_id in self.instances:
                        self.instances[instance_id].state = InstanceState.RUNNING
                        self.instances[instance_id].private_ip = instance_data.get('PrivateIpAddress', '')
                        self.instances[instance_id].public_ip = instance_data.get('PublicIpAddress')
                    logger.info(f"✅ Instance {instance_id} is running")
                    return True
                
                elif state in ['stopped', 'terminated', 'terminating']:
                    logger.error(f"Instance {instance_id} failed to start: {state}")
                    # Check for state reason
                    if 'StateReason' in instance_data:
                        logger.error(f"State reason: {instance_data['StateReason']}")
                    return False
                
                await asyncio.sleep(10)
                
            except self.ec2_client.exceptions.ClientError as e:
                error_code = e.response['Error']['Code']
                if error_code == 'InvalidInstanceID.NotFound':
                    logger.debug(f"Instance {instance_id} not yet available in API, waiting...")
                    await asyncio.sleep(10)
                else:
                    logger.error(f"AWS error checking instance {instance_id}: {e}")
                    await asyncio.sleep(10)
            except Exception as e:
                logger.warning(f"Error checking instance {instance_id}: {e}")
                await asyncio.sleep(10)
        
        logger.error(f"Instance {instance_id} not ready within timeout")
        return False
    
    async def _check_mcp_server_health(self, ip_address: str) -> bool:
        """Check if MCP server is responding on the instance"""
        try:
            async with self.session.get(f"http://{ip_address}:8080/health", timeout=5) as response:
                return response.status == 200
        except:
            return False
    
    async def terminate_instance(self, instance_id: str):
        """Terminate an EC2 instance"""
        try:
            self.ec2_client.terminate_instances(InstanceIds=[instance_id])
            
            if instance_id in self.instances:
                self.instances[instance_id].state = InstanceState.TERMINATED
            
            logger.info(f"Terminated instance {instance_id}")
            
        except Exception as e:
            logger.error(f"Error terminating instance {instance_id}: {e}")
    
    async def auto_scale_instances(self, target_load: int, instance_type: str, ami_id: str, max_instances: int = 10) -> List[str]:
        """Auto-scale instances based on load"""
        try:
            # Calculate required instances
            running_instances = [
                inst for inst in self.instances.values() 
                if inst.state == InstanceState.RUNNING and inst.instance_type == instance_type
            ]
            
            current_capacity = sum(inst.max_load for inst in running_instances)
            required_instances = max(1, min(max_instances, (target_load + 9) // 10))  # 10 images per instance
            
            launched_instances = []
            
            if len(running_instances) < required_instances:
                # Launch additional instances
                instances_to_launch = required_instances - len(running_instances)
                
                for i in range(instances_to_launch):
                    instance_id = await self.launch_instance(instance_type, ami_id)
                    launched_instances.append(instance_id)
                
                # Wait for instances to be ready
                for instance_id in launched_instances:
                    await self.wait_for_instance_ready(instance_id)
            
            logger.info(f"Auto-scaled to {required_instances} instances for load {target_load}")
            return launched_instances
            
        except Exception as e:
            logger.error(f"Error auto-scaling instances: {e}")
            return []
    
    async def distribute_load(self, images: List[str], instance_type: str) -> Dict[str, List[str]]:
        """Distribute image processing load across available instances"""
        try:
            # Get available instances
            available_instances = [
                inst for inst in self.instances.values()
                if inst.state == InstanceState.RUNNING and inst.instance_type == instance_type
            ]
            
            if not available_instances:
                raise Exception("No available instances for load distribution")
            
            # Distribute images evenly
            images_per_instance = len(images) // len(available_instances)
            remainder = len(images) % len(available_instances)
            
            distribution = {}
            start_idx = 0
            
            for i, instance in enumerate(available_instances):
                batch_size = images_per_instance + (1 if i < remainder else 0)
                end_idx = start_idx + batch_size
                
                distribution[instance.instance_id] = images[start_idx:end_idx]
                start_idx = end_idx
            
            logger.info(f"Distributed {len(images)} images across {len(available_instances)} instances")
            return distribution
            
        except Exception as e:
            logger.error(f"Error distributing load: {e}")
            return {}
    
    async def get_cost_summary(self) -> Dict[str, Any]:
        """Get cost summary for all running instances"""
        try:
            total_cost = 0
            instance_costs = {}
            
            current_time = time.time()
            
            for instance_id, instance in self.instances.items():
                if instance.state == InstanceState.RUNNING:
                    runtime_hours = (current_time - instance.launch_time) / 3600
                    cost = runtime_hours * instance.cost_per_hour
                    
                    total_cost += cost
                    instance_costs[instance_id] = {
                        "instance_type": instance.instance_type,
                        "runtime_hours": runtime_hours,
                        "cost_per_hour": instance.cost_per_hour,
                        "total_cost": cost
                    }
            
            return {
                "total_cost": total_cost,
                "instance_costs": instance_costs,
                "active_instances": len([i for i in self.instances.values() if i.state == InstanceState.RUNNING])
            }
            
        except Exception as e:
            logger.error(f"Error calculating costs: {e}")
            return {"error": str(e)}
    
    async def _get_instance_pricing(self, instance_type: str) -> float:
        """Get EC2 instance pricing (2026 us-east-1 on-demand rates)"""
        pricing_map = {
            'm7g.large': 0.082,    # Graviton3 (updated 2026)
            'm7g.xlarge': 0.163,   # Graviton3 (updated 2026)
            'm7g.2xlarge': 0.326,  # Graviton3 (updated 2026)
            'm6g.large': 0.077,
            'm6g.xlarge': 0.154,
            'm6g.2xlarge': 0.308,
            'm6g.4xlarge': 0.616,
            'c7g.large': 0.0725,
            'm7i.large': 0.101,    # x86 (updated 2026)
            'm7i.xlarge': 0.202,   # x86 (updated 2026)
            'c7i.large': 0.085
        }
        return pricing_map.get(instance_type, 0.1)
    
    def _get_opencv_server_code(self) -> str:
        """Return the OpenCV MCP server code to be deployed on instances"""
        # This would contain the actual server code
        # For brevity, returning a placeholder
        return """
# OpenCV MCP Server code would be inserted here
# This includes the full opencv-mcp-server.py content
import asyncio
import json
from mcp.server import Server
# ... rest of the server code
"""
    
    def add_instance_to_pool(self, instance_id: str, instance_type: str, build_mode: str, architecture: str):
        """Add an instance to the reuse pool after benchmark completion"""
        self.instance_pool[instance_id] = {
            "instance_type": instance_type,
            "build_mode": build_mode,
            "architecture": architecture,
            "last_used": time.time(),
            "state": "idle"
        }
        logger.info(f"Added instance {instance_id} to reuse pool ({instance_type}, {build_mode}, {architecture})")
    
    def find_reusable_instance(self, instance_type: str, build_mode: str, architecture: str):
        """Find a reusable instance matching instance_type and architecture.
        
        Ignores build_mode when searching — any idle instance with the same
        instance_type and architecture can be reused.  The caller gets back
        the pool's current build_mode so it can reconfigure the MCP server
        if needed.
        
        Returns:
            tuple (instance_id, pool_build_mode) if found, or (None, None)
        """
        # Iterate over a copy to avoid "dictionary changed size during iteration" error
        for instance_id, config in list(self.instance_pool.items()):
            if (config["instance_type"] == instance_type and 
                config["architecture"] == architecture and
                config["state"] == "idle"):
                
                pool_build_mode = config["build_mode"]
                
                # Check if instance is still running
                try:
                    response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
                    if response['Reservations']:
                        instance_state = response['Reservations'][0]['Instances'][0]['State']['Name']
                        if instance_state == 'running':
                            # Mark as in-use
                            config["state"] = "in-use"
                            config["last_used"] = time.time()
                            
                            # Update termination timestamp to 2 hours from now
                            new_termination_timestamp = str(int(time.time() + 7200))
                            try:
                                self.ec2_client.create_tags(
                                    Resources=[instance_id],
                                    Tags=[{'Key': 'TerminationTimestamp', 'Value': new_termination_timestamp}]
                                )
                                logger.info(f"Updated termination timestamp for {instance_id} to {new_termination_timestamp}")
                            except Exception as tag_error:
                                logger.warning(f"Could not update termination timestamp: {tag_error}")
                            
                            logger.info(f"Reusing instance {instance_id} ({instance_type}, pool_build_mode={pool_build_mode}, requested={build_mode})")
                            return instance_id, pool_build_mode
                        else:
                            # Instance not running, remove from pool
                            logger.warning(f"Instance {instance_id} in pool but not running (state: {instance_state}), removing from pool")
                            del self.instance_pool[instance_id]
                except Exception as e:
                    logger.error(f"Error checking instance {instance_id}: {e}")
                    # Remove from pool if we can't check it
                    del self.instance_pool[instance_id]
        
        return None, None
    
    def release_instance(self, instance_id: str):
        """Release an instance back to the pool after use"""
        if instance_id in self.instance_pool:
            self.instance_pool[instance_id]["state"] = "idle"
            self.instance_pool[instance_id]["last_used"] = time.time()
            logger.info(f"Released instance {instance_id} back to pool")
    
    async def _cleanup_idle_instances_loop(self):
        """Background task to cleanup instances idle for more than 2 hours"""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                
                current_time = time.time()
                instances_to_remove = []
                
                # Iterate over a copy to avoid "dictionary changed size during iteration" error
                for instance_id, config in list(self.instance_pool.items()):
                    if config["state"] == "idle":
                        idle_time = current_time - config["last_used"]
                        
                        if idle_time > self.idle_timeout_seconds:
                            logger.info(f"Instance {instance_id} idle for {idle_time/3600:.1f} hours, terminating...")
                            try:
                                await self.terminate_instance(instance_id)
                                instances_to_remove.append(instance_id)
                                logger.info(f"Terminated idle instance {instance_id}")
                            except Exception as e:
                                logger.error(f"Error terminating idle instance {instance_id}: {e}")
                
                # Remove terminated instances from pool
                for instance_id in instances_to_remove:
                    del self.instance_pool[instance_id]
                
                if instances_to_remove:
                    logger.info(f"Cleaned up {len(instances_to_remove)} idle instances")
                    
            except Exception as e:
                logger.error(f"Error in idle instance cleanup loop: {e}")
    
    async def _cleanup_orphaned_instances(self):
        """Cleanup orphaned instances from previous orchestrator runs"""
        try:
            logger.info("Checking for orphaned benchmark instances...")
            
            # Find all running instances with our project tag
            response = self.ec2_client.describe_instances(
                Filters=[
                    {'Name': 'tag:Project', 'Values': ['OpenCV-Graviton-Benchmark']},
                    {'Name': 'instance-state-name', 'Values': ['running']},
                    {'Name': 'tag:AutoTerminate', 'Values': ['true']}
                ]
            )
            
            current_time = time.time()
            orphaned_count = 0
            
            for reservation in response['Reservations']:
                for instance in reservation['Instances']:
                    instance_id = instance['InstanceId']
                    
                    # Get timestamps from tags
                    launch_timestamp = None
                    termination_timestamp = None
                    max_lifetime_hours = 3  # Default
                    
                    for tag in instance.get('Tags', []):
                        if tag['Key'] == 'LaunchTimestamp':
                            launch_timestamp = int(tag['Value'])
                        elif tag['Key'] == 'TerminationTimestamp':
                            termination_timestamp = int(tag['Value'])
                        elif tag['Key'] == 'MaxLifetimeHours':
                            max_lifetime_hours = int(tag['Value'])
                    
                    should_terminate = False
                    reason = ""
                    
                    # Check termination timestamp (2 hour idle timeout)
                    if termination_timestamp and current_time > termination_timestamp:
                        should_terminate = True
                        idle_hours = (current_time - termination_timestamp) / 3600
                        reason = f"past termination time by {idle_hours:.1f}h"
                    
                    # Check max lifetime (3 hour hard limit)
                    elif launch_timestamp:
                        age_hours = (current_time - launch_timestamp) / 3600
                        if age_hours > max_lifetime_hours:
                            should_terminate = True
                            reason = f"exceeded max lifetime ({age_hours:.1f}h > {max_lifetime_hours}h)"
                    
                    if should_terminate:
                        logger.warning(f"Found orphaned instance {instance_id}: {reason}")
                        try:
                            await self.terminate_instance(instance_id)
                            orphaned_count += 1
                            logger.info(f"Terminated orphaned instance {instance_id}")
                        except Exception as e:
                            logger.error(f"Error terminating orphaned instance {instance_id}: {e}")
            
            if orphaned_count > 0:
                logger.info(f"Cleaned up {orphaned_count} orphaned instances")
            else:
                logger.info("No orphaned instances found")
                
        except Exception as e:
            logger.error(f"Error cleaning up orphaned instances: {e}")
    
    async def cleanup(self):
        """Cleanup resources"""
        try:
            # Terminate all instances
            for instance_id in list(self.instances.keys()):
                await self.terminate_instance(instance_id)
            
            # Close HTTP session
            await self.session.close()
            
            logger.info("Instance manager cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

# Example usage
async def main():
    manager = InstanceManager()
    await manager.initialize()
    
    try:
        # Example: Launch and manage instances
        instance_id = await manager.launch_instance("m7g.large", "ami-12345678")
        ready = await manager.wait_for_instance_ready(instance_id)
        
        if ready:
            print(f"Instance {instance_id} is ready for processing")
        
        # Get cost summary
        costs = await manager.get_cost_summary()
        print(f"Current costs: {costs}")
        
    finally:
        await manager.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
