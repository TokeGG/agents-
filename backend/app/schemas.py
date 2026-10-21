from pydantic import BaseModel


class CreateTaskRequest(BaseModel):
    description: str
    full_auto: bool = False


class CreateTaskResponse(BaseModel):
    id: int
    status: str


class RateTaskRequest(BaseModel):
    value: int
    comment: str | None = None


class AnswerTaskRequest(BaseModel):
    answer: str


class ApproveTaskRequest(BaseModel):
    approved: bool
