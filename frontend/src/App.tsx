import { useState } from "react";
import { createTask } from "./api";
import { TaskView } from "./TaskView";
import { Dashboard } from "./Dashboard";

export function App() {
  const [view, setView] = useState<"create" | "dashboard">("create");
  const [description, setDescription] = useState("");
  const [fullAuto, setFullAuto] = useState(false);
  const [taskIds, setTaskIds] = useState<number[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);

  async function handleSubmit() {
    const { id } = await createTask(description, fullAuto);
    setTaskIds((prev) => [...prev, id]);
    setSelectedTaskId(id);
    setDescription("");
  }

  if (view === "dashboard") {
    return (
      <div>
        <div style={{ padding: 8, background: "#1e1e1e" }}>
          <button onClick={() => setView("create")}>New task</button>
          <button disabled>Dashboard</button>
        </div>
        <Dashboard />
      </div>
    );
  }

  return (
    <div>
      <div>
        <button disabled>New task</button>
        <button onClick={() => setView("dashboard")}>Dashboard</button>
      </div>
      <h1>Agent Core</h1>

      <div>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Describe a task…"
        />
        <label>
          <input type="checkbox" checked={fullAuto} onChange={(e) => setFullAuto(e.target.checked)} />
          Full auto
        </label>
        <button onClick={handleSubmit} disabled={!description}>
          Create task
        </button>
      </div>

      <h2>Tasks</h2>
      <ul>
        {taskIds.map((id) => (
          <li key={id}>
            <button onClick={() => setSelectedTaskId(id)}>Task #{id}</button>
          </li>
        ))}
      </ul>

      {selectedTaskId !== null && <TaskView taskId={selectedTaskId} />}
    </div>
  );
}
