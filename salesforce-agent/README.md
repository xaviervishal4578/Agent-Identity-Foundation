# salesforce-agent

Salesforce specialist **A2A sub-agent**. It searches an organization's **document
library** — other teams' architecture decision records (ADRs) and org-wide
engineering policies uploaded as **Salesforce Files** — via the **Salesforce
REST/SOSL Search API**, authenticating with an app-only **2-legged OAuth
(client-credentials)** flow (no interactive user consent). Salesforce full-text
indexes uploaded `.docx`/`.pdf`/`.pptx` content; the agent downloads the top hits
and extracts a text excerpt around the query. Deployed to **Cloud Run** and
consumed by the root Code Assist agent over A2A.

Generated with `agents-cli` version `0.6.1`.

## Configuration

Copy `.env.example` to `.env` and fill in:

| Variable | Purpose |
| --- | --- |
| `GOOGLE_CLOUD_PROJECT` | GCP project for Vertex AI / logging. |
| `MODEL` | Gemini model id (e.g. `gemini-3.5-flash`). |
| `GOOGLE_CLOUD_LOCATION` | Vertex location for model calls. Use `global` — these Gemini models are served from the global endpoint. |
| `SALESFORCE_DOMAIN` | Org My Domain host, no scheme (e.g. `mycompany-dev-ed.develop.my.salesforce.com`). |
| `SALESFORCE_CLIENT_ID` | Connected App / External Client App **Consumer Key**. |
| `SALESFORCE_CLIENT_SECRET` | Consumer **Secret**. Prefer Secret Manager in production. |
| `SALESFORCE_API_VERSION` | *(Optional)* REST API version for `/services/data/vXX.X/` (default `v62.0`). |
| `SALESFORCE_TOKEN_TTL_SECONDS` | *(Optional)* Fallback cache lifetime; Salesforce omits `expires_in` for this flow (default 3600). |

> **App + file access:** enable the OAuth **client-credentials** flow on the app
> with a **Run-As user**, and give that user read access to the files (grant
> **Query All Files** + **View All Data**, or add it to the files' Library). The
> token endpoint is `https://<SALESFORCE_DOMAIN>/services/oauth2/token`; the flow
> returns an `access_token` and `instance_url` (no `refresh_token`, no
> `expires_in`).

At deploy time, inject the secrets rather than shipping `.env`:

```bash
agents-cli deploy --update-env-vars \
  SALESFORCE_DOMAIN=...,SALESFORCE_CLIENT_ID=...,SALESFORCE_CLIENT_SECRET=...
```

## Populating the document library

The agent searches **Salesforce Files**, so upload the corpus (the ADR/policy
documents) to the org: App Launcher > **Files** > **Upload Files** (drag-and-drop
the `.docx`). The Run-As user must be able to read them (grant **Query All Files**,
or add the user to the files' Library).

Salesforce's file search index lags **~15 minutes** before uploads are findable via
SOSL (a `SELECT COUNT() FROM ContentVersion` SOQL query confirms they exist
immediately).

## Result snippets

SOSL returns matching files but no content snippet, so the top hits are enriched:
the agent downloads each file's `VersionData` and builds a **~1500-char excerpt**
around the matched query term. `.docx`/`.pdf`/`.txt`/`.md` are supported (via
`python-docx` / `pypdf`); other types return an empty snippet (the title still
helps).

## Long-term memory (Vertex AI Memory Bank)

The agent ships with long-term-memory code (`PreloadMemoryTool` +
`add_session_to_memory`). Provision a Memory Bank and set `MEMORY_BANK_ID`:

```bash
uv run python scripts/create_memory_bank.py   # prints MEMORY_BANK_ID
```

Without `MEMORY_BANK_ID` it runs as a standalone search agent (in-memory service,
no recall across conversations). Memory Bank uses a regional endpoint, so keep
`MEMORY_BANK_LOCATION=us-central1` even though the model stays on `global`.

## A2A integration (consumed by code-assistant)

This agent's A2A card is served at:

```
{CLOUD_RUN_BASE_URL}/a2a/app/.well-known/agent-card.json
```

The root **code-assistant** agent connects to it exactly like its other ADK
sub-agents (see `agents/code-assistant/app/agent.py`). Set a
`SALESFORCE_AGENT_URL` env var on code-assistant and add:

```python
SALESFORCE_AGENT_URL = os.environ["SALESFORCE_AGENT_URL"]  # {base}/a2a/app/.well-known/agent-card.json

salesforce_agent = RemoteA2aAgent(
    name="salesforce_agent",
    description="Searches other teams' ADRs and org-wide engineering policies in the Salesforce document library.",
    agent_card=SALESFORCE_AGENT_URL,
    use_legacy=False,  # ADK A2A extension, same as github_agent
)
```

Then include `salesforce_agent` among the root agent's sub-agents / tools.

## Project Structure

```
salesforce-agent/
├── app/         # Core agent code
│   ├── agent.py               # Main agent logic
│   ├── salesforce_client.py   # 2LO client-credentials + SOSL Files search + text extract
│   ├── tools.py               # search_salesforce tool (token caching, 401 re-auth)
│   ├── fast_api_app.py        # FastAPI Backend server
│   └── app_utils/             # App utilities and helpers
├── scripts/
│   └── create_memory_bank.py  # Provision the Vertex AI Memory Bank
├── tests/                     # Unit and integration tests
└── pyproject.toml             # Project dependencies
```
