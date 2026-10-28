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

"""Tests for the A2A request converter that scopes runs to the real user id."""

from types import SimpleNamespace

from google.adk.a2a.converters.request_converter import AgentRunRequest

from app.app_utils import a2a


def _patch_default(monkeypatch, base):
    """Replace the default converter used inside _user_scoped_request_converter."""
    monkeypatch.setattr(
        a2a,
        "convert_a2a_request_to_agent_run_request",
        lambda request, part_converter: base,
    )


def test_overrides_user_id_from_forwarded_metadata(monkeypatch) -> None:
    """A forwarded user_id in request.metadata becomes the ADK run user_id."""
    base = AgentRunRequest(user_id="A2A_USER_ctx-123", session_id="ctx-123")
    _patch_default(monkeypatch, base)
    request = SimpleNamespace(metadata={"user_id": "dauren@google.com"})

    result = a2a._user_scoped_request_converter(request)

    assert result.user_id == "dauren@google.com"
    # session stays per-conversation
    assert result.session_id == "ctx-123"


def test_falls_back_when_no_metadata(monkeypatch) -> None:
    """Without a forwarded user_id, keep the default A2A_USER_{context_id}."""
    base = AgentRunRequest(user_id="A2A_USER_ctx-123", session_id="ctx-123")
    _patch_default(monkeypatch, base)
    request = SimpleNamespace(metadata=None)

    result = a2a._user_scoped_request_converter(request)

    assert result.user_id == "A2A_USER_ctx-123"


def test_falls_back_when_metadata_has_no_user_id(monkeypatch) -> None:
    """Metadata present but without user_id -> keep the default."""
    base = AgentRunRequest(user_id="A2A_USER_ctx-123", session_id="ctx-123")
    _patch_default(monkeypatch, base)
    request = SimpleNamespace(metadata={"something_else": "x"})

    result = a2a._user_scoped_request_converter(request)

    assert result.user_id == "A2A_USER_ctx-123"
