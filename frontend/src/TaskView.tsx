import { useEffect, useState } from "react";
import { answerTask, approveTask, getTask, rateTask, streamTask, TaskDetail } from "./api";

export function TaskView({ taskId }: { taskId: number }) {
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [answer, setAnswer] = useState("");

  useEffect(() => {
    let cancelled = false;
    getTask(taskId).then((d) => {
      if (!cancelled) setDetail(d);
    });
    const stop = streamTask(taskId, (event) => {
      setDetail((prev) =>
        prev ? { ...prev, events: [...prev.events, event] } : prev
      );
      // Status changes (completed/awaiting_answer/awaiting_approval) are only
      // reflected in task.status, which the stream doesn't carry directly —
      // re-fetch the task on every event to keep status in sync.
      getTask(taskId).then((d) => {
        if (!cancelled) setDetail(d);
      });
    });
    return () => {
      cancelled = true;
      stop();
    };
  }, [taskId]);

  if (!detail) return <div>Loading…</div>;

  const { task, events } = detail;
  const latestEvent = events[events.length - 1];

  return (
    <div>
      <h2>Task #{task.id}</h2>
      <p>{task.description}</p>
      <p>Status: {task.status}</p>

      <ul>
        {events.map((event, i) => (
          <li key={i}>
            <strong>{event.type}</strong>: {JSON.stringify(event.payload)}
          </li>
        ))}
      </ul>

      {task.status === "awaiting_answer" && latestEvent?.type === "ask_user" && (
        <div>
          <p>Question: {String(latestEvent.payload.question)}</p>
          <input value={answer} onChange={(e) => setAnswer(e.target.value)} />
          <button
            onClick={async () => {
              await answerTask(task.id, answer);
              setAnswer("");
            }}
          >
            Answer
          </button>
        </div>
      )}

      {task.status === "awaiting_approval" && (
        <div>
          <p>Approval requested.</p>
          <button onClick={() => approveTask(task.id, true)}>Approve</button>
          <button onClick={() => approveTask(task.id, false)}>Deny</button>
        </div>
      )}

      {task.status === "completed" && (
        <div>
          <button onClick={() => rateTask(task.id, 1)}>👍</button>
          <button onClick={() => rateTask(task.id, -1)}>👎</button>
        </div>
      )}
    </div>
  );
}
