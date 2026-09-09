from fastapi import FastAPI

from app.api.routes import router

app = FastAPI(title="Product Intelligence V1")
app.include_router(router)
