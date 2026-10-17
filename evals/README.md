# Sewa Setu Evaluation Harness

This directory contains the automated evaluation suite for testing the Sewa Setu AI Assistant across 16 personas representing real-world citizens, edge cases, adversarial attacks, and government reviewing officers.

---

## 1. Architecture & Isolation

Evaluations execute **strictly** against the isolated **staging stack** (`sewasetu-staging`).
- **Production Stack:** Runs on port `8080` (with production database volume `pgdata` and upload store).
- **Staging Stack:** Runs on port `8081` (with isolated database volume `staging_pgdata` and upload store `staging_uploads`).
- **Safety Lock:** Database resets via `scripts/reset_eval_state.py` verify database metadata and strictly refuse to execute against any non-staging database.

---

## 2. Quickstart

### Step 1: Start the Staging Stack
```bash
docker compose -p sewasetu-staging --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml up -d --build
```
Verify staging is up:
```bash
curl http://localhost:8081/healthz
```

### Step 2: Configure Anthropic API Key
Set your API key in the environment:
```bash
# Windows PowerShell
$env:ANTHROPIC_API_KEY="sk-ant-api03-..."

# Linux / macOS
export ANTHROPIC_API_KEY="sk-ant-api03-..."
```
*(Optional: Set custom models via `ASSISTANT_MODEL` and `PERSONA_MODEL`. Defaults to `claude-haiku-5-5`).*

For offline dry-runs or sandbox verification without external egress, you can add `--mock` to use the deterministic simulation engine.

---

## 3. Running Evaluations

### Run a Single Citizen Persona (e.g. Persona 1: Kalpana Das)
```bash
python evals/run_eval.py --persona 1 --runs 1 --label smoke
```

### Run a Single Officer Persona (e.g. Persona 13: Reviewing Officer)
```bash
python evals/run_eval.py --persona 13 --runs 1 --label smoke
```

### Run All 16 Personas
```bash
python evals/run_eval.py --persona all --runs 1 --label full
```

---

## 4. Reading Results

Outputs are recorded under `evals/runs/<timestamp>-<label>/results.json`.

Each run file contains:
- **Run Header:** Git commit SHA, models, date, staging URL, temperature.
- **Summary:** Total test runs, pass/fail counts, overall boolean status.
- **Detailed Persona Results:**
  - `assertions`: List of deterministic PostgreSQL assertions (`expected` vs `actual`).
  - `soft_metrics`: Tool error count, whether assistant read back summary before submitting, whether assistant accurately stated Rs 250 amount and 31 Oct 2026 deadline.
  - `transcript`: Complete interaction history with all sensitive OAuth tokens, JWTs, upload tokens, and SMS OTPs automatically redacted.
  - `tool_calls`: Log of every MCP tool invocation with arguments and sanitized results.
