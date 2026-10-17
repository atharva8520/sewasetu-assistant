#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Evaluation Harness for Sewa Setu AI Assistant.
Runs evaluation personas against the isolated staging stack.

CLI:
  python evals/run_eval.py --persona 1 --runs 1 --label smoke
  python evals/run_eval.py --persona 13 --runs 1 --label smoke
  python evals/run_eval.py --persona all --label full
"""

import os
import sys
import json
import time
import asyncio
import argparse
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
import re
import yaml
import requests
# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from evals.lib.dates import resolve_template_sql, get_today_ist
from evals.lib.oauth import perform_citizen_oauth, perform_officer_oauth, revoke_refresh_token
from evals.lib.mcp_client import EvalMcpClient
from evals.lib.llm import LLMRunner, DEFAULT_ASSISTANT_MODEL, DEFAULT_PERSONA_MODEL, FIXED_TEMPERATURE
from evals.lib.evaluator import evaluate_expected_db, compute_soft_metrics, redact_sensitive
from evals.lib.db import run_staging_psql

IST = ZoneInfo("Asia/Kolkata")


def get_git_commit() -> str:
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception:
        return "unknown"


def load_personas(yaml_path: str = "evals/personas.yaml") -> list[dict]:
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("personas", [])


async def run_single_persona(persona: dict, base_url: str, runner: LLMRunner) -> dict:
    pid = persona["id"]
    pname = persona["name"]
    role = persona.get("role", "citizen")
    mobile = persona.get("mobile", "9000000001")
    max_turns = persona.get("max_turns", 15)
    start_time = time.time()

    print(f"\n=======================================================")
    print(f"[*] Running Persona {pid}: {pname} (Role: {role}, Mobile: {mobile})")
    print(f"=======================================================")

    # 1. Reset eval state for this mobile and any setup mobiles
    mobiles_to_clear = set(persona.get("mobiles", [mobile]))
    for q in persona.get("setup", []):
        for found in re.findall(r"'(90000\d{5})'", q):
            mobiles_to_clear.add(found)

    for m in sorted(mobiles_to_clear):
        subprocess.run(
            [sys.executable, "scripts/reset_eval_state.py", "--mobile", m],
            check=True,
            capture_output=True,
        )

    # 2. Apply setup SQL if any
    setup_queries = persona.get("setup", [])
    for query in setup_queries:
        resolved_query = resolve_template_sql(query, get_today_ist())
        run_staging_psql(resolved_query)

    # 3. OAuth Authentication
    tokens = {}
    if role == "citizen":
        tokens = perform_citizen_oauth(base_url, mobile)
    else:
        tokens = perform_officer_oauth(base_url)

    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")
    client_id = tokens["client"]["client_id"]

    # 4. Connect MCP Client over streamable HTTP
    mcp_endpoint = f"{base_url}/mcp/{role}"
    mcp_client = EvalMcpClient(mcp_endpoint, access_token)
    await mcp_client.connect()
    anthropic_tools = mcp_client.get_anthropic_tools()

    # 5. Dialogue loop
    dialogue_history = []
    assistant_messages = []
    tool_calls_log = []
    turns_completed = 0
    goal_reached = False

    # Special handling for Persona 11: token revocation trigger after draft save
    persona_11_revoked = False

    try:
        # Initial user opening
        user_msg = f"Hello, I am {pname}."
        if role == "citizen":
            user_msg = f"Namaskar, my name is {pname} and I want to apply for the pension scheme."
        elif role == "officer":
            user_msg = "Please review the pending applications queue and process any cases requiring decision."

        for turn_idx in range(1, max_turns + 1):
            turns_completed = turn_idx
            print(f"--- Turn {turn_idx}/{max_turns} ---")

            # Append user message
            dialogue_history.append({"role": "user", "content": user_msg})
            assistant_messages.append({"role": "user", "content": user_msg})

            # Assistant response loop (handles multi-step tool calls within one turn)
            while True:
                resp = await runner.call_assistant(assistant_messages, anthropic_tools)
                assistant_text = resp["text"]
                tool_calls = resp["tool_calls"]
                content_blocks = resp["content"]

                # Append assistant message to assistant thread
                assistant_messages.append({"role": "assistant", "content": content_blocks})
                if assistant_text:
                    dialogue_history.append({"role": "assistant", "content": assistant_text})
                    print(f"Assistant: {assistant_text[:120]}...")

                # If no tool calls, assistant turn is complete
                if not tool_calls:
                    break

                # Execute tool calls via MCP client
                tool_results_content = []
                for tc in tool_calls:
                    t_name = tc["name"]
                    t_args = tc["input"]
                    print(f"  [Tool Call] {t_name}({t_args})")
                    try:
                        t_res = await mcp_client.call_tool(t_name, t_args)
                    except Exception as err:
                        t_res = f"Error calling tool: {err}"
                    print(f"  [Tool Result] {t_res[:100]}...")

                    tool_calls_log.append({
                        "turn": turn_idx,
                        "name": t_name,
                        "arguments": t_args,
                        "result": t_res,
                    })
                    tool_results_content.append({
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": t_res,
                    })

                    # Handle automatic document upload if upload link received
                    if (
                        t_name == "get_upload_link"
                        and persona.get("has_document", False)
                    ):
                        m_link = re.search(r"(http[^\s\"']+)", t_res)
                        if m_link:
                            upload_url = m_link.group(1).rstrip(".,;")
                            print(f"  [*] Persona uploading document to {upload_url}...")
                            try:
                                dummy_jpeg = (
                                    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
                                    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
                                    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
                                    b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342"
                                    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4"
                                    b"\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00"
                                    b"\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
                                    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
                                )
                                r_up = requests.post(upload_url, files={"document": ("proof.jpg", dummy_jpeg, "image/jpeg")}, timeout=5)
                                print(f"  [+] Document upload response: {r_up.status_code}")
                            except Exception as up_err:
                                print(f"  [-] Document upload failed: {up_err}")

                    # Persona 11 trap: revoke refresh token after draft save
                    if pid == 11 and t_name == "save_draft" and not persona_11_revoked:
                        print("  [*] Triggering Persona 11 session revocation...")
                        revoke_refresh_token(base_url, refresh_token, client_id)
                        persona_11_revoked = True

                # Send tool results back to assistant
                assistant_messages.append({"role": "user", "content": tool_results_content})

            # Check if assistant announced goal achieved
            last_asst_text = dialogue_history[-1]["content"] if dialogue_history else ""
            if "[GOAL_ACHIEVED]" in last_asst_text:
                goal_reached = True
                break

            # Run persona simulator turn
            user_reply = await runner.call_persona(persona, dialogue_history)
            print(f"Persona: {user_reply[:120]}")

            if "[GOAL_ACHIEVED]" in user_reply:
                goal_reached = True
                break
            if "[GIVE_UP]" in user_reply:
                break

            user_msg = user_reply

    finally:
        await mcp_client.close()

    elapsed = time.time() - start_time

    # 6. Evaluate expected_db with plain Python assertions
    eval_res = evaluate_expected_db(persona)
    passed = eval_res["passed"]

    # 7. Compute soft metrics
    soft_metrics = compute_soft_metrics(dialogue_history, tool_calls_log)

    # 8. Sanitize transcript
    sanitized_transcript = []
    for item in dialogue_history:
        sanitized_transcript.append({
            "role": item["role"],
            "text": redact_sensitive(item.get("content", "")),
        })

    sanitized_tool_calls = []
    for tc in tool_calls_log:
        sanitized_tool_calls.append({
            "turn": tc["turn"],
            "name": tc["name"],
            "arguments": json.loads(redact_sensitive(json.dumps(tc["arguments"]))),
            "result": redact_sensitive(tc["result"]),
        })

    print(f"\n[Result] Persona {pid} {'PASS' if passed else 'FAIL'} in {elapsed:.2f}s ({turns_completed} turns)")
    for a in eval_res["assertions"]:
        status = "OK" if a["passed"] else "FAIL"
        print(f"  - [{status}] {a['name']}: expected={a['expected']}, actual={a['actual']}")

    return {
        "persona_id": pid,
        "persona_name": pname,
        "role": role,
        "passed": passed,
        "turns_completed": turns_completed,
        "time_seconds": round(elapsed, 2),
        "assertions": eval_res["assertions"],
        "soft_metrics": soft_metrics,
        "transcript": sanitized_transcript,
        "tool_calls": sanitized_tool_calls,
    }


def main():
    parser = argparse.ArgumentParser(description="Sewa Setu Persona Evaluation Harness.")
    parser.add_argument("--persona", default="1", help="Persona ID to run (e.g. 1, 13, or 'all')")
    parser.add_argument("--runs", type=int, default=1, help="Number of runs per persona")
    parser.add_argument("--label", default="smoke", help="Label for run directory")
    parser.add_argument("--base-url", default=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8081"), help="Staging Base URL")
    parser.add_argument("--mock", action="store_true", help="Run with deterministic offline simulation engine")
    args = parser.parse_args()

    # Model configuration
    assistant_model = os.environ.get("ASSISTANT_MODEL", DEFAULT_ASSISTANT_MODEL)
    persona_model = os.environ.get("PERSONA_MODEL", DEFAULT_PERSONA_MODEL)

    # Initialize LLM runner (fails clearly if ANTHROPIC_API_KEY missing and --mock not set)
    runner = LLMRunner(allow_mock=args.mock)

    # Load personas
    all_personas = load_personas()
    if args.persona.lower() == "all":
        personas_to_run = all_personas
    else:
        try:
            pid = int(args.persona)
            personas_to_run = [p for p in all_personas if p["id"] == pid]
        except ValueError:
            print(f"Invalid --persona value: {args.persona}", file=sys.stderr)
            sys.exit(1)

    if not personas_to_run:
        print(f"No personas found matching --persona {args.persona}", file=sys.stderr)
        sys.exit(1)

    # Prepare output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("evals", "runs", f"{timestamp}-{args.label}")
    os.makedirs(out_dir, exist_ok=True)

    header = {
        "timestamp": timestamp,
        "git_commit": get_git_commit(),
        "staging_url": args.base_url,
        "assistant_model": assistant_model,
        "persona_model": persona_model,
        "temperature": FIXED_TEMPERATURE,
        "label": args.label,
        "mode": "mock" if runner.is_mock else "live",
    }

    results = []
    overall_pass = True

    for p in personas_to_run:
        for r_num in range(1, args.runs + 1):
            res = asyncio.run(run_single_persona(p, args.base_url, runner))
            res["run_index"] = r_num
            results.append(res)
            if not res["passed"]:
                overall_pass = False

    output_payload = {
        "header": header,
        "summary": {
            "total_runs": len(results),
            "passed": sum(1 for r in results if r["passed"]),
            "failed": sum(1 for r in results if not r["passed"]),
            "all_passed": overall_pass,
        },
        "results": results,
    }

    out_file = os.path.join(out_dir, "results.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2)

    print(f"\n=======================================================")
    print(f"EVALUATION COMPLETE")
    print(f"Saved results to: {out_file}")
    print(f"Total: {len(results)} | Passed: {sum(1 for r in results if r['passed'])} | Failed: {sum(1 for r in results if not r['passed'])}")
    print(f"=======================================================")

    if not overall_pass:
        sys.exit(1)


if __name__ == "__main__":
    main()
