# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Offline config tests for the Salesforce sub-agent (no model/network calls)."""

from google.adk.tools.authenticated_function_tool import AuthenticatedFunctionTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from app.agent import app, generate_memories_callback, root_agent


def test_agent_identity() -> None:
    """The agent and app are named as the runner expects."""
    assert root_agent.name == "salesforce_agent"
    # App name must match the agent directory for the runner to find sessions.
    assert app.name == "app"


def test_tools_are_wired() -> None:
    """The agent exposes the AuthenticatedFunctionTool plus the memory preloader."""
    assert len(root_agent.tools) == 2
    assert isinstance(root_agent.tools[0], AuthenticatedFunctionTool)
    assert root_agent.tools[0].name == "search_salesforce"
    assert isinstance(root_agent.tools[1], PreloadMemoryTool)


def test_memory_generation_callback_wired() -> None:
    """The after-agent callback that persists memories is attached."""
    assert root_agent.after_agent_callback is generate_memories_callback
