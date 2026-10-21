from fastapi import FastAPI

from app.api.events import router as events_router
from app.api.tasks import router as tasks_router

app = FastAPI()
app.include_router(tasks_router)
app.include_router(events_router)
