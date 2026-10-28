import os

from a2a.server.apps import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, EXTENDED_AGENT_CARD_PATH
from fastapi import FastAPI
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
from google.adk.artifacts import InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from agent import app as adk_app
from agent import root_agent

APP_NAME = "agent"


def _resolve_app_url() -> str:
    if env_url := os.getenv("APP_URL"):
        return env_url
    agent_engine_id = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID")
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", "us-central1")
    if agent_engine_id and project and location:
        return (
            f"https://{location}-aiplatform.googleapis.com/reasoningEngines/v1"
            f"/projects/{project}/locations/{location}"
            f"/reasoningEngines/{agent_engine_id}/api"
        )
    return "http://0.0.0.0:8080"


app = FastAPI(title="minimal-github-agent")
runner = Runner(
    app=adk_app,
    session_service=InMemorySessionService(),
    artifact_service=InMemoryArtifactService(),
    auto_create_session=True,
)


@app.on_event("startup")
async def _setup_a2a() -> None:
    app_url = _resolve_app_url()
    rpc_path = f"/a2a/{APP_NAME}"

    agent_card = await AgentCardBuilder(
        agent=root_agent,
        capabilities=AgentCapabilities(streaming=True),
        rpc_url=f"{app_url}{rpc_path}",
        agent_version=os.getenv("AGENT_VERSION", "0.1.0"),
    ).build()

    request_handler = DefaultRequestHandler(
        agent_executor=A2aAgentExecutor(runner=runner),
        task_store=InMemoryTaskStore(),
    )
    a2a_app = A2AFastAPIApplication(agent_card=agent_card, http_handler=request_handler)
    a2a_app.add_routes_to_app(
        app,
        agent_card_url=f"{rpc_path}{AGENT_CARD_WELL_KNOWN_PATH}",
        rpc_url=rpc_path,
        extended_agent_card_url=f"{rpc_path}{EXTENDED_AGENT_CARD_PATH}",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
