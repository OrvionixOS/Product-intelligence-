from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router

app = FastAPI(title="Product Intelligence V1")
app.include_router(router)

# Milestone 7B: the workflow interface. A static mount rather than a route, so
# it adds nothing to the API surface and cannot be mistaken for one. No build
# step and no framework: the page calls the same endpoints any other client
# would, which keeps the UI honest by construction -- it can only show what the
# API actually returns.
WEB_ROOT = Path(__file__).parent / "web"
app.mount("/app", StaticFiles(directory=WEB_ROOT, html=True), name="web")
