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

"""Offline tests for the memory service selection and generation callback."""

import pytest
from google.adk.memory import InMemoryMemoryService, VertexAiMemoryBankService

from app.agent import generate_memories_callback
from app.app_utils import services


def _reset_cache() -> None:
    services.get_memory_service.cache_clear()


def test_memory_service_defaults_to_in_memory(monkeypatch) -> None:
    """Without MEMORY_BANK_ID (or empty), fall back to the in-memory service."""
    monkeypatch.delenv("MEMORY_BANK_ID", raising=False)
    _reset_cache()
    assert isinstance(services.get_memory_service(), InMemoryMemoryService)
    _reset_cache()


def test_memory_service_uses_vertex_when_configured(monkeypatch) -> None:
    """With MEMORY_BANK_ID set, use Vertex AI Memory Bank at the agent location."""
    monkeypatch.setenv("MEMORY_BANK_ID", "1234567890")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    _reset_cache()
    assert isinstance(services.get_memory_service(), VertexAiMemoryBankService)
    _reset_cache()


class _FakeCallbackContext:
    """Minimal stand-in for ADK's CallbackContext for the generation callback."""

    def __init__(self) -> None:
        self.session_saved = False

    async def add_session_to_memory(self) -> None:
        self.session_saved = True


@pytest.mark.asyncio
async def test_generate_callback_saves_full_session() -> None:
    """The callback saves the whole session to memory (matches ADK quickstart).

    Saving the session (not a trailing event window) is what keeps the user's
    message in scope on multi-event tool turns.
    """
    ctx = _FakeCallbackContext()
    await generate_memories_callback(ctx)
    assert ctx.session_saved is True
