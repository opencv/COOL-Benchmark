# COOL Benchmark : OpenCV on AWS Graviton

A benchmarking system that measures OpenCV image-processing performance across AWS Graviton (ARM64) EC2 instances. It spins up real EC2 instances, runs a configurable image pipeline in volatile memory (no S3/disk bottlenecks), and reports throughput, latency, and cost side-by-side.

---

## How It Works

1. The **orchestrator** (`shared/benchmark-orchestrator.py`) exposes a REST API on port `8080`. It receives benchmark requests from the frontend, launches EC2 instances via the AWS SDK, runs the OpenCV pipeline over SSH/SSM, collects results, and terminates instances when done.
2. The **frontend** (`frontend/serve.py`) serves a static web UI on port `3000` that lets you configure and trigger benchmarks, watch live progress, and compare results.
3. The **image search agent** (`agents/image-search-agent.py`) is an MCP server that fetches public images on demand (NASA, medical datasets, etc.) into volatile memory for the benchmark workload.

---

## Prerequisites

- Python 3.10+
- An AWS account with permission to launch EC2 instances (e.g. :`t4g`, `m7g`, `c7g`, `m6i` families)
- An EC2 key pair in your target region
- An [Anthropic API key](https://console.anthropic.com/) for the image search agent
- The AWS Marketplace OpenCV Graviton AMI ID for your region

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/opencv/COOL-Benchmark
cd COOL-Benchmark
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv cool
source cool/bin/activate        # Linux / macOS
# cool\Scripts\activate         # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create your marketplace config

```bash
cp config-marketplace.example.json config-marketplace.json
```

Open `config-marketplace.json` and replace `ami-xxxxxxxxxxxxxxxxx` with the OpenCV Graviton AMI ID from AWS Marketplace for your region. You can also set this later through the UI

---

## Export Environment Variables

Set these before starting either process. Open a terminal and run:

```bash
# AWS credentials — used by the orchestrator to launch EC2 instances
export AWS_ACCESS_KEY_ID="YOUR_AWS_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="YOUR_AWS_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="us-east-1"          # change to your preferred region

# EC2 key pair name — must already exist in the region above
export EC2_KEY_PAIR_NAME="your-ec2-key-pair-name"

# Anthropic API key — used by the auto-retry feature to analyse build errors
export ANTHROPIC_API_KEY="sk-ant-api03-..."

# (Optional) Pre-configure the Marketplace AMI ID instead of entering it in the UI
# export MARKETPLACE_AMI_ID="ami-xxxxxxxxxxxxxxxxx"
```

---

## Run

Open **two terminals**, both with the environment variables above already exported.

### Terminal 1 : Start the backend orchestrator

```bash
cd COOL-Benchmark
python shared/benchmark-orchestrator.py
```

Expected output:
```
INFO  benchmark-orchestrator - Benchmark orchestrator started on http://0.0.0.0:8080
```

### Terminal 2 : Start the frontend server

```bash
cd COOL-Benchmark
python frontend/serve.py
```

Expected output:
```
Frontend server running at http://localhost:3000/
Press Ctrl+C to stop
```

### Open the UI

Navigate to [http://localhost:3000](http://localhost:3000) in your browser.

---

## Project Structure

```
COOL-Benchmark/
├── shared/
│   ├── benchmark-orchestrator.py       # Main backend — REST API + EC2 orchestration
│   ├── benchmark_executor.py           # Per-instance benchmark runner
│   ├── build_manager.py                # OpenCV build/install logic on remote instances
│   ├── auto_retry_manager.py           # LLM-powered auto-retry for build failures
│   └── shared_instance_benchmark.py
├── agentcore/
│   └── instance-manager.py             # EC2 lifecycle management (launch, pool, terminate)
├── opencv-ami/
│   ├── opencv-mcp-server.py            # MCP server deployed to every EC2 instance at runtime
│   └── install-opencv-optimized.sh     # Graviton-optimised OpenCV build script (used by compile-from-source option)
├── agents/
│   ├── image-search-agent.py           # MCP server — fetches public images into memory
│   └── requirements.txt
├── frontend/
│   ├── serve.py                        # Static file server (port 3000)
│   ├── index.html                      # Main UI
│   ├── app.js                          # Frontend logic
│   └── styles.css
├── requirements.txt                    # All Python dependencies
├── config-marketplace.example.json     # Template — copy to config-marketplace.json and fill in your AMI ID
└── README.md
```

---

## Performance Metrics Reported

- **Processing duration** (seconds per scenario)
- **Throughput** (images per second)
- **Cost estimate** (USD, live AWS pricing)
- **Instance count** (active at peak)
- **Real-time image preview** (cycles through processed results)

---

## Troubleshooting

**Orchestrator fails to launch instances**

- Confirm `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_DEFAULT_REGION` are exported.
- Verify your IAM user/role has `ec2:RunInstances`, `ec2:DescribeInstances`, `ec2:TerminateInstances`, and `ssm:SendCommand` permissions.
- Check that `EC2_KEY_PAIR_NAME` matches an existing key pair in the configured region.

**Auto-retry does not start**

- Ensure `ANTHROPIC_API_KEY` is set before submitting a benchmark run.

**Frontend shows "Cannot connect to backend"**

- Make sure the orchestrator is running and listening on port `8080` before opening the UI.
- Both processes must be in the same network context (local machine or same EC2 instance).

