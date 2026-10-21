import { useState } from "react";
import { createTask } from "./api";
import { TaskView } from "./TaskView";

export function App() {
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

  return (
    <div>
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
