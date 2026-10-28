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

"""Salesforce tool: app-only 2-Legged OAuth (2LO) Files search via Auth Manager.

Token Exchange Lifecycle:
-------------------------
Instead of storing `SALESFORCE_CLIENT_ID` and `SALESFORCE_CLIENT_SECRET` in
environment variables and manually performing OAuth token exchanges or session
caching in application code, this module delegates the 2-legged OAuth (2LO)
client-credentials flow to Google Cloud Agent Identity Auth Manager:

1. Provider Registration & AuthConfig:
   `CredentialManager.register_auth_provider(GcpAuthProvider())` registers the
   Google Cloud Auth Manager integration at import time, and `salesforce_auth_config`
   binds `GcpAuthProviderScheme` to the `salesforce-2lo-auth-provider` URI.
2. Tool Interception (`AuthenticatedFunctionTool`):
   Wrapping `search_salesforce` in `AuthenticatedFunctionTool` instructs the ADK
   to authenticate to Auth Manager using the Cloud Run service's Google Cloud
   Service Account (via Application Default Credentials) before every tool invocation.
3. Credential Injection & Extraction (`_extract_token`):
   Auth Manager performs the 2LO client-credentials grant (or returns a refreshed
   token) and the ADK injects an `AuthCredential` object directly into the tool
   function's `credential` parameter (hidden from the LLM's tool schema).
   `_extract_token` normalizes the resolved token across OAuth2 and HTTP bearer
   credential structures and passes it to `SalesforceClient.search_files`.
"""

import os

from google.adk.auth.auth_credential import AuthCredential
from google.adk.auth.auth_tool import AuthConfig
from google.adk.auth.credential_manager import CredentialManager
from google.adk.integrations.agent_identity import (
    GcpAuthProvider,
    GcpAuthProviderScheme,
)
from google.adk.tools.authenticated_function_tool import AuthenticatedFunctionTool

from app.salesforce_client import SalesforceClient

# Full resource name of the 2LO auth provider in Agent Identity Auth Manager.
SALESFORCE_AUTH_PROVIDER_URI = os.environ["SALESFORCE_AUTH_PROVIDER_URI"]

# Step 1: Register the Google Cloud auth provider and build the AuthConfig
# referencing the 2LO auth provider resource URI.
CredentialManager.register_auth_provider(GcpAuthProvider())
salesforce_auth_config = AuthConfig(
    auth_scheme=GcpAuthProviderScheme(name=SALESFORCE_AUTH_PROVIDER_URI)
)


def _extract_token(credential: AuthCredential) -> str | None:
    """Extracts the raw access token from the AuthCredential resolved by Auth Manager.

    Handles OAuth2 credentials (`credential.oauth2.access_token`), standard HTTP
    bearer credentials (`credential.http.credentials.token`), and custom HTTP
    authorization headers so the tool remains resilient across provider formats.
    """
    if not credential:
        return None
    if credential.oauth2 and credential.oauth2.access_token:
        return credential.oauth2.access_token
    if (
        credential.http
        and credential.http.credentials
        and credential.http.credentials.token
    ):
        return credential.http.credentials.token
    if credential.http and credential.http.additional_headers:
        h = credential.http.additional_headers
        return (
            h.get("X-API-Key")
            or h.get("X-GOOG-API-KEY")
            or h.get("Authorization", "").replace("Bearer ", "")
            or None
        )
    return None


def build_search_salesforce(client: SalesforceClient):
    """Builds the Salesforce search tool bound to a credential-free client.

    Args:
        client: A credential-free ``SalesforceClient`` (My Domain + API version).

    Returns:
        The ``search_salesforce`` ``AuthenticatedFunctionTool`` for the agent.
    """

    # Step 2: Accept the ADK-injected `credential: AuthCredential` parameter
    # alongside the model-supplied `query: str` argument.
    def search_salesforce(query: str, credential: AuthCredential) -> dict:
        """Searches the company's Salesforce document library (Files).

        Runs a SOSL full-text search across every file the application can read;
        the caller does not need to know a file name or folder.

        Args:
            query: The search term or issue description.

        Returns:
            A dict with a 'status' key; on success, 'data' holds matching
            documents (each a {'name', 'url', 'snippet'} dict). On failure,
            'error_message' explains why.
        """
        access_token = _extract_token(credential)
        if not access_token:
            return {
                "status": "error",
                "error_message": "Salesforce auth failed: no token from Auth Manager.",
            }

        try:
            documents = client.search_files(access_token, query)
        except Exception as e:  # surface any search failure to the model
            return {
                "status": "error",
                "error_message": f"Salesforce search failed: {e}",
            }

        return {
            "status": "success",
            "query": query,
            "data": documents,
        }

    # Step 3: Wrap `search_salesforce` in `AuthenticatedFunctionTool` so the ADK
    # resolves `salesforce_auth_config` and injects `credential` before each call.
    return AuthenticatedFunctionTool(
        func=search_salesforce, auth_config=salesforce_auth_config
    )
