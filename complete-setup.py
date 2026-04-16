#!/usr/bin/env python3
"""
Complete setup: Apply policy and create IAM role for instances
"""
import boto3
import json
import sys

def apply_policy():
    """Apply the fixed IAM policy"""
    print("="*70)
    print("STEP 1: Applying Fixed IAM Policy")
    print("="*70)
    
    iam = boto3.client('iam')
    user_name = 'OpenCVCOOLBenchmarkUser'
    policy_name = 'OpenCVBenchmarkPolicy'
    
    try:
        with open('fixed-policy.json', 'r') as f:
            policy_document = json.load(f)
        
        iam.put_user_policy(
            UserName=user_name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(policy_document)
        )
        print(f"✅ Policy applied to {user_name}")
        return True
    except Exception as e:
        print(f"❌ Failed to apply policy: {e}")
        return False

def create_iam_role():
    """Create OpenCVInstanceRole if it doesn't exist"""
    print("\n" + "="*70)
    print("STEP 2: Creating IAM Role for EC2 Instances")
    print("="*70)
    
    iam = boto3.client('iam')
    role_name = 'OpenCVInstanceRole'
    
    # Trust policy for EC2
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }
    
    # Check if role exists
    try:
        iam.get_role(RoleName=role_name)
        print(f"✅ Role {role_name} already exists")
    except iam.exceptions.NoSuchEntityException:
        print(f"Creating role {role_name}...")
        try:
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description='IAM role for OpenCV benchmark EC2 instances to use SSM'
            )
            print(f"✅ Role created")
        except Exception as e:
            print(f"❌ Failed to create role: {e}")
            return False
    except Exception as e:
        print(f"❌ Error checking role: {e}")
        return False
    
    # Attach SSM policy
    print(f"\nAttaching AmazonSSMManagedInstanceCore policy...")
    try:
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn='arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore'
        )
        print(f"✅ Policy attached")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"✅ Policy already attached")
    except Exception as e:
        print(f"❌ Failed to attach policy: {e}")
        return False
    
    # Create instance profile
    print(f"\nCreating instance profile...")
    try:
        iam.get_instance_profile(InstanceProfileName=role_name)
        print(f"✅ Instance profile already exists")
    except iam.exceptions.NoSuchEntityException:
        try:
            iam.create_instance_profile(InstanceProfileName=role_name)
            print(f"✅ Instance profile created")
            
            # Add role to profile
            print(f"Adding role to instance profile...")
            iam.add_role_to_instance_profile(
                InstanceProfileName=role_name,
                RoleName=role_name
            )
            print(f"✅ Role added to instance profile")
        except Exception as e:
            print(f"❌ Failed to create instance profile: {e}")
            return False
    except Exception as e:
        print(f"❌ Error checking instance profile: {e}")
        return False
    
    return True

def verify_setup():
    """Verify the setup is complete"""
    print("\n" + "="*70)
    print("STEP 3: Verifying Setup")
    print("="*70)
    
    iam = boto3.client('iam')
    
    # Check role
    try:
        role = iam.get_role(RoleName='OpenCVInstanceRole')
        print(f"✅ Role exists: {role['Role']['Arn']}")
    except Exception as e:
        print(f"❌ Role check failed: {e}")
        return False
    
    # Check instance profile
    try:
        profile = iam.get_instance_profile(InstanceProfileName='OpenCVInstanceRole')
        print(f"✅ Instance profile exists: {profile['InstanceProfile']['Arn']}")
    except Exception as e:
        print(f"❌ Instance profile check failed: {e}")
        return False
    
    # Check attached policies
    try:
        policies = iam.list_attached_role_policies(RoleName='OpenCVInstanceRole')
        ssm_attached = any(p['PolicyName'] == 'AmazonSSMManagedInstanceCore' for p in policies['AttachedPolicies'])
        if ssm_attached:
            print(f"✅ SSM policy attached to role")
        else:
            print(f"⚠️  SSM policy not attached")
    except Exception as e:
        print(f"⚠️  Could not check policies: {e}")
    
    return True

def main():
    print("\n" + "="*70)
    print("COMPLETE SETUP FOR MCP SERVER DEPLOYMENT")
    print("="*70)
    print("\nThis script will:")
    print("  1. Apply the fixed IAM policy to your user")
    print("  2. Create OpenCVInstanceRole for EC2 instances")
    print("  3. Attach SSM permissions to the role")
    print("  4. Create instance profile")
    print("\n" + "="*70 + "\n")
    
    # Step 1: Apply policy
    if not apply_policy():
        print("\n❌ Setup failed at step 1")
        return False
    
    # Step 2: Create IAM role
    if not create_iam_role():
        print("\n❌ Setup failed at step 2")
        return False
    
    # Step 3: Verify
    if not verify_setup():
        print("\n❌ Setup failed at step 3")
        return False
    
    # Success!
    print("\n" + "="*70)
    print("✅ SETUP COMPLETE!")
    print("="*70)
    print("\nNext steps:")
    print("  1. Terminate current instance (has no IAM role):")
    print("     python3 << 'EOF'")
    print("import boto3")
    print("ec2 = boto3.client('ec2', region_name='us-east-1')")
    print("ec2.terminate_instances(InstanceIds=['i-0d02dde1d2d9a7064'])")
    print("print('✅ Instance terminated')")
    print("EOF")
    print("\n  2. Launch a new benchmark from the frontend")
    print("  3. New instance will have:")
    print("     ✅ IAM role attached")
    print("     ✅ SSM agent connected")
    print("     ✅ MCP server deployed automatically")
    print("     ✅ Port 8080 open")
    print("\n  4. Verify with: python3 diagnose-mcp-issue.py <new-instance-id>")
    print("\n" + "="*70)
    
    return True

if __name__ == '__main__':
    try:
        success = main()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
