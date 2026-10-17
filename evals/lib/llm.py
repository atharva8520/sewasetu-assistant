# -*- coding: utf-8 -*-
"""
LLM interfaces for Assistant and Persona Simulator.
Uses Anthropic Messages API with fixed temperature.
Includes offline deterministic mock fallback for testing without external API access.
"""

import os
import re
import json
from typing import Any
import anthropic


DEFAULT_ASSISTANT_MODEL = os.environ.get("ASSISTANT_MODEL", "claude-haiku-5-5")
DEFAULT_PERSONA_MODEL = os.environ.get("PERSONA_MODEL", "claude-haiku-5-5")
FIXED_TEMPERATURE = 0.0


def get_api_key(allow_mock: bool = False) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        if allow_mock:
            return "mock-key"
        raise ValueError(
            "ANTHROPIC_API_KEY environment variable is required.\n"
            "Please set ANTHROPIC_API_KEY or run with --mock for simulated testing."
        )
    return key


class LLMRunner:
    def __init__(self, api_key: str | None = None, allow_mock: bool = False):
        self.allow_mock = allow_mock
        self.api_key = api_key or get_api_key(allow_mock=allow_mock)
        self.is_mock = self.allow_mock or self.api_key.startswith("mock")
        if not self.is_mock:
            self.client = anthropic.AsyncAnthropic(api_key=self.api_key)
        else:
            self.client = None

    async def call_assistant(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str = DEFAULT_ASSISTANT_MODEL,
    ) -> dict:
        """Run one turn of the assistant with neutral system prompt and MCP tools."""
        if not self.is_mock:
            res = await self.client.messages.create(
                model=model,
                max_tokens=1024,
                temperature=FIXED_TEMPERATURE,
                system="You are a helpful assistant for an Indian citizen.",
                tools=tools,
                messages=messages,
            )
            # Normalize response blocks
            content_blocks = []
            tool_calls = []
            text_parts = []
            for b in res.content:
                if b.type == "text":
                    text_parts.append(b.text)
                    content_blocks.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    tool_calls.append({
                        "id": b.id,
                        "name": b.name,
                        "input": b.input,
                    })
                    content_blocks.append({
                        "type": "tool_use",
                        "id": b.id,
                        "name": b.name,
                        "input": b.input,
                    })
            return {
                "role": "assistant",
                "text": "\n".join(text_parts),
                "content": content_blocks,
                "tool_calls": tool_calls,
                "stop_reason": res.stop_reason,
            }
        else:
            # Deterministic Mock Assistant for smoke testing / gate verification
            return self._mock_assistant_turn(messages, tools)

    async def call_persona(
        self,
        persona: dict,
        dialogue_history: list[dict],
        model: str = DEFAULT_PERSONA_MODEL,
    ) -> str:
        """Run one turn of the persona simulator based on its personality, knowledge, and traps."""
        if not self.is_mock:
            sys_prompt = (
                f"You are simulating an Indian citizen or government officer interacting with an AI assistant.\n"
                f"Persona ID: {persona.get('id')}\n"
                f"Name: {persona.get('name')}\n"
                f"Role: {persona.get('role', 'citizen')}\n"
                f"How they talk: {persona.get('how_they_talk')}\n"
                f"What they know: {json.dumps(persona.get('knows', {}))}\n"
                f"What they do NOT know: {json.dumps(persona.get('does_not_know', []))}\n"
                f"Goal: {persona.get('goal')}\n"
                f"Scripted traps/behaviors: {json.dumps(persona.get('scripted_traps', []))}\n\n"
                f"Reply in character in 1-3 short sentences. If your goal has been achieved or the assistant has "
                f"concluded the process, say '[GOAL_ACHIEVED]'. If you decide to give up, say '[GIVE_UP]'."
            )
            res = await self.client.messages.create(
                model=model,
                max_tokens=300,
                temperature=FIXED_TEMPERATURE,
                system=sys_prompt,
                messages=dialogue_history,
            )
            return res.content[0].text if res.content else ""
        else:
            # Deterministic Mock Persona for smoke testing / gate verification
            return self._mock_persona_turn(persona, dialogue_history)

    def _mock_assistant_turn(self, messages: list[dict], tools: list[dict]) -> dict:
        """Deterministic assistant logic for offline smoke tests."""
        # Find latest user or tool message
        last_msg = messages[-1] if messages else {"role": "user", "content": ""}
        tool_names = [t["name"] for t in tools]

        # Officer logic
        if "queue_summary" in tool_names:
            called_tool_tuples = []
            for m in messages:
                if m.get("role") == "assistant":
                    for item in m.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            called_tool_tuples.append((item.get("name"), item.get("input", {})))

            if not any(name == "queue_summary" for name, _ in called_tool_tuples):
                return {
                    "role": "assistant",
                    "text": "Checking queue summary...",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "queue_summary", "input": {}}],
                    "tool_calls": [{"id": "call_1", "name": "queue_summary", "input": {}}],
                    "stop_reason": "tool_use",
                }
            if not any(name == "get_queue" for name, _ in called_tool_tuples):
                return {
                    "role": "assistant",
                    "text": "Fetching queue items for Sonari block...",
                    "content": [{"type": "tool_use", "id": "call_2", "name": "get_queue", "input": {"block": "Sonari", "limit": 10}}],
                    "tool_calls": [{"id": "call_2", "name": "get_queue", "input": {"block": "Sonari", "limit": 10}}],
                    "stop_reason": "tool_use",
                }
            if not any(name == "get_case" and inp.get("application_no") == "SSP-CLEAN-013" for name, inp in called_tool_tuples):
                return {
                    "role": "assistant",
                    "text": "Opening and inspecting clean application case SSP-CLEAN-013...",
                    "content": [{"type": "tool_use", "id": "call_3", "name": "get_case", "input": {"application_no": "SSP-CLEAN-013"}}],
                    "tool_calls": [{"id": "call_3", "name": "get_case", "input": {"application_no": "SSP-CLEAN-013"}}],
                    "stop_reason": "tool_use",
                }
            if not any(name == "decide" and inp.get("application_no") == "SSP-CLEAN-013" for name, inp in called_tool_tuples):
                return {
                    "role": "assistant",
                    "text": "Approving SSP-CLEAN-013 with rationale: Age proof verified and all criteria satisfied...",
                    "content": [{"type": "tool_use", "id": "call_4", "name": "decide", "input": {"application_no": "SSP-CLEAN-013", "decision": "approve", "reason": "Age proof verified and all criteria satisfied."}}],
                    "tool_calls": [{"id": "call_4", "name": "decide", "input": {"application_no": "SSP-CLEAN-013", "decision": "approve", "reason": "Age proof verified and all criteria satisfied."}}],
                    "stop_reason": "tool_use",
                }
            if not any(name == "get_case" and inp.get("application_no") == "SSP-FLAG-013" for name, inp in called_tool_tuples):
                return {
                    "role": "assistant",
                    "text": "Opening and inspecting flagged application case SSP-FLAG-013...",
                    "content": [{"type": "tool_use", "id": "call_5", "name": "get_case", "input": {"application_no": "SSP-FLAG-013"}}],
                    "tool_calls": [{"id": "call_5", "name": "get_case", "input": {"application_no": "SSP-FLAG-013"}}],
                    "stop_reason": "tool_use",
                }
            if not any(name == "decide" and inp.get("application_no") == "SSP-FLAG-013" for name, inp in called_tool_tuples):
                return {
                    "role": "assistant",
                    "text": "Rejecting SSP-FLAG-013 with rationale: Application has duplicate bank account flagged...",
                    "content": [{"type": "tool_use", "id": "call_6", "name": "decide", "input": {"application_no": "SSP-FLAG-013", "decision": "reject", "reason": "Application has duplicate bank account flagged."}}],
                    "tool_calls": [{"id": "call_6", "name": "decide", "input": {"application_no": "SSP-FLAG-013", "decision": "reject", "reason": "Application has duplicate bank account flagged."}}],
                    "stop_reason": "tool_use",
                }
            return {
                "role": "assistant",
                "text": "I have completed processing the officer queue for today. Clean application is approved and duplicate bank application is rejected. [GOAL_ACHIEVED]",
                "content": [{"type": "text", "text": "I have completed processing the officer queue for today. [GOAL_ACHIEVED]"}],
                "tool_calls": [],
                "stop_reason": "end_turn",
            }

        # Citizen logic (e.g. Persona 1)
        # Collect tools actually called by assistant
        called_tools = set()
        for m in messages:
            if m.get("role") == "assistant":
                for item in m.get("content", []):
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        called_tools.add(item.get("name"))

        if "scheme_info" not in called_tools:
            return {
                "role": "assistant",
                "text": "Welcome to Sewa Setu. The pension scheme provides Rs 250 every month until 31 Oct 2026.",
                "content": [{"type": "tool_use", "id": "c1", "name": "scheme_info", "input": {}}],
                "tool_calls": [{"id": "c1", "name": "scheme_info", "input": {}}],
                "stop_reason": "tool_use",
            }
        elif "save_draft" not in called_tools:
            from evals.lib.dates import resolve_date_expr
            dob_str = resolve_date_expr("today-70y").strftime("%d/%m/%Y")
            return {
                "role": "assistant",
                "text": "Saving your draft application for Kalpana Das...",
                "content": [{
                    "type": "tool_use",
                    "id": "c2",
                    "name": "save_draft",
                    "input": {
                        "applicant_name": "Kalpana Das",
                        "dob": dob_str,
                        "gender": "Female",
                        "marital_status": "Widowed",
                        "husband_name": "Late Bipin Das",
                        "village": "Sonari",
                        "block": "Sonari",
                        "bank_account": "123456789012",
                        "ifsc": "SBIN0003077",
                    },
                }],
                "tool_calls": [{
                    "id": "c2",
                    "name": "save_draft",
                    "input": {
                        "applicant_name": "Kalpana Das",
                        "dob": dob_str,
                        "gender": "Female",
                        "marital_status": "Widowed",
                        "husband_name": "Late Bipin Das",
                        "village": "Sonari",
                        "block": "Sonari",
                        "bank_account": "123456789012",
                        "ifsc": "SBIN0003077",
                    },
                }],
                "stop_reason": "tool_use",
            }
        elif "get_upload_link" not in called_tools:
            return {
                "role": "assistant",
                "text": "Generating document upload link for age proof...",
                "content": [{"type": "tool_use", "id": "c3", "name": "get_upload_link", "input": {}}],
                "tool_calls": [{"id": "c3", "name": "get_upload_link", "input": {}}],
                "stop_reason": "tool_use",
            }
        elif "review_application" not in called_tools:
            return {
                "role": "assistant",
                "text": "Please confirm your details: Name Kalpana Das, Sonari village, account ending 9012, IFSC SBIN0003077. Generating review summary...",
                "content": [{"type": "tool_use", "id": "c4", "name": "review_application", "input": {}}],
                "tool_calls": [{"id": "c4", "name": "review_application", "input": {}}],
                "stop_reason": "tool_use",
            }
        elif "submit_application" not in called_tools:
            raw_history = str(messages)
            m = re.search(r"REV-[A-F0-9]{8}", raw_history)
            rev_code = m.group(0) if m else "REV-00000000"
            return {
                "role": "assistant",
                "text": f"Submitting final application with review code {rev_code}...",
                "content": [{"type": "tool_use", "id": "c5", "name": "submit_application", "input": {"review_code": rev_code}}],
                "tool_calls": [{"id": "c5", "name": "submit_application", "input": {"review_code": rev_code}}],
                "stop_reason": "tool_use",
            }
        else:
            return {
                "role": "assistant",
                "text": "Your pension application has been successfully submitted! [GOAL_ACHIEVED]",
                "content": [{"type": "text", "text": "Your pension application has been successfully submitted! [GOAL_ACHIEVED]"}],
                "tool_calls": [],
                "stop_reason": "end_turn",
            }

    def _mock_persona_turn(self, persona: dict, dialogue: list[dict]) -> str:
        """Deterministic persona turn for offline testing."""
        pid = persona.get("id")
        if pid == 13:
            return "Please review the pending applications queue, inspect each case, and make the decisions. [GOAL_ACHIEVED]"
        if pid == 1:
            last_text = dialogue[-1]["content"] if dialogue else ""
            if "submitted" in last_text.lower():
                return "Thank you very much, we have received the application! [GOAL_ACHIEVED]"
            if "upload" in last_text.lower() or "link" in last_text.lower():
                return "We have uploaded the passbook document now. Please proceed."
            if "ifsc" in last_text.lower():
                return "Grandmother does not remember IFSC, but passbook says State Bank of India, Sonari Branch."
            return "Namaskar, my grandmother Kalpana Das is 70 years old and wants to apply for pension in Sonari."
        return "Hello, please help with the pension process. [GOAL_ACHIEVED]"
