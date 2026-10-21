export type CreateTaskResponse = { id: number; status: string };

export type TaskDetail = {
  task: { id: number; description: string; status: string; full_auto: boolean };
  events: { type: string; payload: Record<string, unknown> }[];
};

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return response.json();
}

export function createTask(description: string, fullAuto: boolean): Promise<CreateTaskResponse> {
  return postJson("/tasks", { description, full_auto: fullAuto });
}

export async function getTask(id: number): Promise<TaskDetail> {
  const response = await fetch(`/tasks/${id}`);
  return response.json();
}

export function rateTask(id: number, value: number, comment?: string): Promise<{ id: number }> {
  return postJson(`/tasks/${id}/rate`, { value, comment });
}

export function answerTask(id: number, answer: string): Promise<{ id: number; status: string }> {
  return postJson(`/tasks/${id}/answer`, { answer });
}

export function approveTask(id: number, approved: boolean): Promise<{ id: number; status: string }> {
  return postJson(`/tasks/${id}/approve`, { approved });
}

export function streamTask(id: number, onEvent: (event: { type: string; payload: Record<string, unknown> }) => void): () => void {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/tasks/${id}/stream`);
  socket.onmessage = (message) => {
    onEvent(JSON.parse(message.data));
  };
  return () => socket.close();
}
