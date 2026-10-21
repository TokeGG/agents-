export type CreateTaskResponse = { id: number; status: string };

export type TaskDetail = {
  task: { id: number; description: string; status: string; full_auto: boolean };
  events: { type: string; payload: Record<string, unknown> }[];
};

// Stub bearer-token auth (see backend/app/auth.py). Set at build time via
// VITE_AUTH_TOKEN; full login (passkey/2FA) is deploy-time work, not here.
function authToken(): string {
  return import.meta.env.VITE_AUTH_TOKEN ?? "";
}

function authHeaders(): Record<string, string> {
  return { Authorization: `Bearer ${authToken()}` };
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  return response.json();
}

export function createTask(description: string, fullAuto: boolean): Promise<CreateTaskResponse> {
  return postJson("/tasks", { description, full_auto: fullAuto });
}

export async function getTask(id: number): Promise<TaskDetail> {
  const response = await fetch(`/tasks/${id}`, { headers: authHeaders() });
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
  const socket = new WebSocket(
    `${protocol}//${window.location.host}/tasks/${id}/stream?token=${encodeURIComponent(authToken())}`
  );
  socket.onmessage = (message) => {
    onEvent(JSON.parse(message.data));
  };
  return () => socket.close();
}
