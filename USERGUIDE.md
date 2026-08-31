# Defrag — User Guide

A complete step-by-step guide to setting up and running Defrag from a fresh clone.

---

## Part 1 — What This Does

Defrag is an AI-powered multi-agent system that:

1. **Discovers** your cloud resources (AWS EC2, RDS, Lambda, S3) and subscriptions (Stripe, Render)
2. **Understands** which resources belong to which project (using tags, naming patterns, and GitHub)
3. **Analyzes** usage, billing, and activity
4. **Assesses** the risk and urgency of each project (via LLM reasoning)
5. **Recommends** safe actions (KEEP, MONITOR, RENEW, STOP, ESCALATE)
6. **Waits for your approval** before doing anything consequential
7. **Executes** the approved action (EC2 STOP — reversible)
8. **Verifies** the resulting state

Think of it as an AI co-pilot for your cloud bill.

---

## Part 2 — System Requirements

| Requirement | Minimum | Recommended |
|---|---|---|
| OS | macOS 12+ / Ubuntu 22+ | macOS 14+ |
| Python | 3.11 | 3.12 |
| pip | 23+ | Latest |
| RAM | 1 GB | 2 GB |
| AWS | Optional | Optional |
| GitHub | Optional | Optional |
| LLM API | Optional (rule-based fallback) | Gemini or OpenAI key |

---

## Part 3 — Installation

### Step 1: Clone the repository

```bash
git clone <your-repo-url>
cd Defrag
```

### Step 2: Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
```

### Step 3: Install dependencies

```bash
pip install -r requirements.txt
```

This installs FastAPI, SQLAlchemy, boto3, PyGithub, stripe, google-genai, and all other dependencies.

### Step 4: Configure environment

```bash
cp .env.example .env
```

Open `.env` in a text editor and fill in the credentials you have (see Part 4).

---

## Part 4 — API Keys / Credentials

| Credential | Required? | Variable | Where to Get | Min Permissions | Used For |
|---|---|---|---|---|---|
| Gemini API key | ⭐ Recommended | `GEMINI_API_KEY` | [makersuite.google.com](https://makersuite.google.com/app/apikey) | Default | LLM reasoning for all agents |
| OpenAI API key | Alternative | `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com/api-keys) | Default | Alternative LLM |
| AWS Access Key | Optional | `AWS_ACCESS_KEY_ID` | AWS IAM Console | See below | Real EC2/RDS/Lambda discovery |
| AWS Secret Key | Optional | `AWS_SECRET_ACCESS_KEY` | AWS IAM Console | See below | Real AWS access |
| GitHub Token | Optional | `GITHUB_TOKEN` | github.com/settings/tokens | `read:user`, `public_repo` | Project context from repos |
| Stripe API Key | Optional | `STRIPE_API_KEY` | Stripe Dashboard | Read-only | Subscription discovery |
| Render API Key | Optional | `RENDER_API_KEY` | Render Dashboard → Account → API Keys | Read | Service discovery |

### Which credentials for which mode?

| Mode | Credentials Needed |
|---|---|
| **A. Simulation** | None (works with empty .env) |
| **B. LLM reasoning** | `GEMINI_API_KEY` or `OPENAI_API_KEY` |
| **C. Real AWS discovery** | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` + LLM |
| **D. Real AWS STOP action** | Same + EC2 `StopInstances` permission |
| **E. GitHub context** | `GITHUB_TOKEN` |

### 🔴 Security Rule: Never expose server credentials to the frontend

All credentials are **server-side only**. The frontend never sees them — it only calls the local API (`/api/*` endpoints running on your machine). Never put AWS keys or API keys in JavaScript or HTML files.

---

## Part 5 — AWS Setup (If Using Real Mode)

### 1. Create an IAM user with least-privilege permissions

In AWS IAM Console, create a new user and attach this policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeRegions",
        "ec2:StopInstances",
        "rds:DescribeDBInstances",
        "rds:ListTagsForResource",
        "lambda:ListFunctions",
        "lambda:ListTags",
        "s3:ListBuckets",
        "s3:GetBucketTagging",
        "cloudwatch:GetMetricStatistics"
      ],
      "Resource": "*"
    }
  ]
}
```

### 2. Get the access key

In IAM → Users → Your user → Security credentials → Create access key. Copy both the **Access Key ID** and **Secret Access Key**.

### 3. Add to .env

```
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
```

### 4. Test AWS connection

```bash
source venv/bin/activate
python -c "import boto3; ec2=boto3.client('ec2', region_name='us-east-1'); print(ec2.describe_regions(RegionNames=['us-east-1']))"
```

### 5. Safe testing with EC2

⚠️ **NEVER test against production EC2 instances you cannot afford to stop.**

Best practice:
1. Create a **test** EC2 instance (`t3.micro`)
2. Tag it: `Name=defrag-test`, `env=test`
3. Run a Defrag scan
4. Approve the STOP action for only this test instance
5. Verify it stopped; restart it from the console

---

## Part 6 — GitHub Setup

### 1. Create a Personal Access Token

Go to github.com → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token.

Required scopes:
- `read:user` — to get your username
- `public_repo` — to list public repositories (or `repo` for private repos)

### 2. Add to .env

```
GITHUB_TOKEN=ghp_your_token_here
```

### 3. Verify

```bash
python -c "from github import Github; g=Github('YOUR_TOKEN'); print(g.get_user().login)"
```

---

## Part 7 — LLM Setup

### Option A: Google Gemini (Recommended)

1. Go to [makersuite.google.com/app/apikey](https://makersuite.google.com/app/apikey)
2. Create a new API key
3. Add to `.env`:

```
GEMINI_API_KEY=AIzaSy...
```

Model used: `gemini-2.0-flash` (configurable via `LLM_MODEL`)

### Option B: OpenAI

1. Go to [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
2. Create a key
3. Add to `.env`:

```
OPENAI_API_KEY=sk-...
```

### No LLM (Simulation/Rule-based mode)

Leave both `GEMINI_API_KEY` and `OPENAI_API_KEY` empty. The system falls back to deterministic rule-based reasoning. The UI and evaluation both work correctly.

---

## Part 8 — Database

### Default: SQLite (no setup required)

The database file `defrag.db` is created automatically when the backend starts.

### Optional: PostgreSQL via Docker

```bash
docker-compose up -d
```

Then set in `.env`:

```
DATABASE_URL=postgresql://orchestrator_user:password123@localhost:5432/orchestrator_db
```

---

## Part 9 — Starting the App

### Start the backend

```bash
source venv/bin/activate
uvicorn api:app --reload --port 8000
```

You should see:

```
INFO: Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

### Open the frontend

Open your browser and go to:

```
http://localhost:8000
```

The dashboard will load. The backend serves the frontend directly — there is no separate frontend server.

---

## Part 10 — First Run

### Step 1: Open the dashboard

Go to `http://localhost:8000`. You'll see the empty state with a **Start Scan** button.

### Step 2: Trigger a scan

Click **🔍 Scan Resources**. A loading overlay will appear showing the agent pipeline running:
- 🔍 Discovery Agent
- 🧩 Inference Agent
- ⚠️ Risk Agent
- 💡 Optimization Agent
- ✅ Execution Agent (dry-run)

Wait 10–30 seconds. The scan runs in the background.

### Step 3: Explore projects & risks

Click the **Projects & Risks** tab. Each project card shows:
- Risk level (LOW/MEDIUM/HIGH/CRITICAL)
- Agent reasoning (LLM or rule-based)
- Safety flags
- Recommended action

### Step 4: View resources

Click the **Resources** tab. Every resource shows its provider, type, cost, CPU, and whether it's real or simulated.

### Step 5: Review action plans

Click the **Action Plans** tab. Each plan shows:
- What actions will be taken
- Monthly savings
- Rollback strategy
- Reasoning

### Step 6: Inspect the agent trajectory

Click the **Agent Trajectory** tab. Every agent step is shown with:
- Which agent ran
- What it did
- Evidence gathered
- Decision made
- Timestamp

### Step 7: Approve a plan (only for non-escalated plans with STOP actions)

If an action plan has an **⚡ Approve & Execute** button:
1. Click it
2. Enter your name or email (for the audit log)
3. Check the confirmation checkbox
4. Click **✅ Approve & Execute**

The backend will execute the plan and show the result.

### Step 8: Observe verification

After execution, go to the **Agent Trajectory** tab. The `ExecutionAgent` step will show the actual EC2 state after stopping.

---

## Part 11 — Simulation Mode

Simulation mode works **without any cloud credentials**. Leave `.env` empty and start the backend normally.

The system will:
1. Load 5 realistic AWS resources (EC2, RDS, Lambda) with varied scenarios
2. Load 3 realistic subscriptions (imminent renewals)
3. Run the full LLM agent pipeline on this data
4. Display results with **SIMULATED** badges on all resources

The UI shows a yellow banner:  
> 🔬 Simulation Mode — No cloud credentials detected. Running realistic scenario data through the real LLM agent pipeline.

All agent reasoning is real (if `GEMINI_API_KEY` is set); only the resource data is simulated.

---

## Part 12 — Real Mode

To switch from simulation to real mode:

1. Add your AWS credentials to `.env`
2. Restart the backend: `Ctrl+C` then `uvicorn api:app --port 8000`
3. Click **Scan Resources** again

The status badge in the header will change from `Mode: SIMULATION` to `Mode: REAL`.

Real and simulated resources can coexist if only some providers are configured.

---

## Part 13 — Evaluation

### Run the evaluation (no credentials needed)

```bash
source venv/bin/activate
python eval_suite.py
```

This runs 20 test cases through both the baseline and the real agent system. Expected output:

```
DEFRAG EVALUATION: Baseline vs. Multi-Agent System
...
Baseline Accuracy:   7/20 (35.0%)
Agent Accuracy:      9/20 (45.0%)
Improvement:         +10.0 percentage points
Unsafe Baseline:     2 cases
Unsafe Agent:        0 cases
```

Results are saved to `eval_results.json`.

### With LLM

Add `GEMINI_API_KEY` to `.env` and re-run:

```bash
python eval_suite.py
```

The agent uses real LLM reasoning for all 20 cases. Accuracy will be higher.

---

## Part 14 — Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| Backend won't start | Port 8000 occupied | `lsof -i :8000` then `kill -9 <PID>` |
| `ModuleNotFoundError` | Dependencies missing | `pip install -r requirements.txt` |
| LLM not working | Wrong API key format | Check `GEMINI_API_KEY` starts with `AIza` |
| AWS auth failure | Wrong credentials | Run `aws sts get-caller-identity` |
| GitHub auth failure | Invalid token | Check token scopes include `read:user` |
| Scan stuck | Slow LLM response | Wait 30s; LLM calls can take time |
| "No scan results" | Scan not triggered | Click Scan Resources first |
| Database error | Permission issue | Check write permission in project directory |
| CORS error | Frontend on different port | Always use `http://localhost:8000` |
| Approval rejected | `approved_by` is empty | Enter your name in the approval form |
| Execution BLOCKED | Plan requires escalation | Human investigation required first |

---

## Part 15 — Safety

### Human approval model

Defrag will **never** automatically stop, terminate, or delete any real resource. The only way a real resource can be stopped is:

1. A scan runs (discovery → inference → risk → optimization → **dry-run only**)
2. You manually click **⚡ Approve & Execute**
3. You enter your name/email
4. You check the confirmation checkbox
5. The backend validates your approval is human (not automated)

### What actions are safe to approve?

| Action | Reversible? | Auto-executable? |
|---|---|---|
| EC2 STOP | ✅ Yes | ✅ Yes (after human approval) |
| EC2 TERMINATE | ❌ No | ❌ Not implemented |
| RDS BACKUP | ✅ Yes | ❌ Logged only |
| Subscription RENEW | ✅ Yes | ❌ Manual only |
| Subscription CANCEL | Depends | ❌ Manual only |

### Production resource safeguards

Resources tagged with `env=prod` or `env=production` are flagged with a safety warning and will trigger escalation in most scenarios.

### Simulated vs. real actions

When in simulation mode, STOP actions are simulated (no real EC2 is affected). The UI always shows whether a result is REAL or SIMULATED.

---

## Part 16 — Cleanup

### Stop the backend

Press `Ctrl+C` in the terminal where uvicorn is running.

### Remove the database

```bash
rm -f defrag.db
```

### Stop PostgreSQL (if using Docker)

```bash
docker-compose down
```

### Remove Docker volumes

```bash
docker-compose down -v
```

### Verify EC2 state

After testing STOP:
1. Go to AWS Console → EC2 → Instances
2. Locate the instance you stopped
3. Select it → Instance state → Start instance

### Revoke test credentials

After testing:
1. Go to AWS IAM → Users → Your test user → Security credentials
2. Delete the access key used for testing
3. Optionally delete the IAM user

---

*Generated by the Defrag Release Engineer. For questions, check the GitHub repository issues.*
