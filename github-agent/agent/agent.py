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

"""GitHub Specialist Agent authenticated via Google Cloud Agent Identity Auth Manager.

Architecture & Token Exchange Lifecycle:
----------------------------------------
Instead of embedding a static `GITHUB_PERSONAL_ACCESS_TOKEN` in `.env` or container
environment variables, this agent resolves its GitHub MCP credential dynamically
at tool-call time using its Vertex AI Agent Runtime SPIFFE workload identity:

1. Provider Registration (`CredentialManager.register_auth_provider`):
   Registers `GcpAuthProvider` once at module import so the ADK knows how to
   communicate with the Google-managed Agent Identity Auth Manager API.
2. Auth Scheme Binding (`GcpAuthProviderScheme`):
   Declares the full resource URI of the API-key auth provider
   (`projects/<PROJECT>/locations/<LOCATION>/authProviders/github-mcp-auth-provider`)
   and attaches it to the `McpToolset`.
3. Runtime Header Injection (`github_header_provider`):
   Prior to each outbound MCP HTTP request, the ADK exchanges the agent's SPIFFE
   identity (`principal://agents.global.org-...`) for the stored API key and
   stores the resulting `AuthCredential` in the toolset's `auth_config`. The
   `github_header_provider` callback extracts the resolved token and injects it
   as an HTTP `Authorization: Bearer <token>` header.
"""

import os

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.auth.credential_manager import CredentialManager
from google.adk.integrations.agent_identity import GcpAuthProvider, GcpAuthProviderScheme
from google.adk.models import Gemini
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.genai import types

load_dotenv()

MODEL = os.environ["MODEL"]
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
# Full resource name of the API-key auth provider in Agent Identity Auth Manager.
GITHUB_AUTH_PROVIDER_URI = os.environ["GITHUB_AUTH_PROVIDER_URI"]

# Restrict exposed MCP tools to read-only repository and issue queries (least privilege).
GITHUB_TOOL_FILTER = ["search_repositories", "search_issues", "list_issues"]

# Step 1: Register the Google Cloud auth provider with ADK's CredentialManager
# so it can resolve credentials from Agent Identity Auth Manager using the
# agent's own workload identity.
CredentialManager.register_auth_provider(GcpAuthProvider())

# Step 2: Declare the auth scheme pointing to the API-key auth provider URI.
github_auth_scheme = GcpAuthProviderScheme(name=GITHUB_AUTH_PROVIDER_URI)


def github_mcp_toolset() -> McpToolset:
    """Builds the MCP toolset for the remote GitHub server authenticated via Auth Manager."""
    toolset = None

    def github_header_provider(readonly_ctx) -> dict[str, str]:
        """Extracts the Auth Manager credential and returns the HTTP Authorization header."""
        if not toolset or not toolset.get_auth_config():
            return {}
        auth_config = toolset.get_auth_config()
        cred = None
        if readonly_ctx and hasattr(readonly_ctx, "get_credential"):
            cred = readonly_ctx.get_credential(auth_config.credential_key)
        if not cred:
            cred = auth_config.exchanged_auth_credential
        if not cred or not cred.http:
            return {}
        token = None
        if cred.http.credentials and cred.http.credentials.token:
            token = cred.http.credentials.token
        elif cred.http.additional_headers:
            token = cred.http.additional_headers.get(
                "X-API-Key"
            ) or cred.http.additional_headers.get("X-GOOG-API-KEY")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    # Step 3: Bind the connection parameters, auth scheme, read-only tool filter,
    # and dynamic header provider into the McpToolset (no static PAT in config).
    toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=GITHUB_MCP_URL),
        auth_scheme=github_auth_scheme,
        tool_filter=GITHUB_TOOL_FILTER,
        header_provider=github_header_provider,
    )
    return toolset


root_agent = Agent(
    name="github_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Specialized assistant for GitHub: searches repositories, issues, and "
        "pull requests via the GitHub MCP server."
    ),
    instruction=(
        "You are a specialized assistant for interacting with GitHub. "
        "Use the provided tools to search for repositories, find issues, and "
        "retrieve pull-request information, then answer with what you find. "
        "Be concise and cite the repository names / issue numbers you used."
    ),
    tools=[github_mcp_toolset()],
)

app = App(root_agent=root_agent, name="app")
