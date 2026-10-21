import { useEffect, useState } from "react";
import {
  answerTask,
  approveTask,
  DashboardResponse,
  DashboardTask,
  getDashboard,
  killTask,
  setKillSwitch,
} from "./api";

const POLL_INTERVAL_MS = 3000;

function TaskCard({ task, onChange }: { task: DashboardTask; onChange: () => void }) {
  const [answer, setAnswer] = useState("");

  return (
    <div
      style={{
        background: "#1e1e1e",
        border: "1px solid #333",
        borderRadius: 6,
        padding: 12,
        marginBottom: 8,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <strong>
          #{task.id} · {task.agent_type}
        </strong>
        <span style={{ color: "#999", fontSize: 12 }}>{task.status}</span>
      </div>
      <p style={{ margin: "4px 0" }}>{task.description}</p>

      {task.status === "awaiting_answer" && (
        <div style={{ display: "flex", gap: 6 }}>
          <input
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            placeholder="Answer…"
            style={{ flex: 1, background: "#121212", color: "#eee", border: "1px solid #444" }}
          />
          <button
            onClick={async () => {
              await answerTask(task.id, answer);
              setAnswer("");
              onChange();
            }}
          >
            Answer
          </button>
        </div>
      )}

      {task.status === "awaiting_approval" && (
        <div style={{ display: "flex", gap: 6 }}>
          <button
            onClick={async () => {
              await approveTask(task.id, true);
              onChange();
            }}
          >
            Approve
          </button>
          <button
            onClick={async () => {
              await approveTask(task.id, false);
              onChange();
            }}
          >
            Deny
          </button>
        </div>
      )}

      {(task.status === "running" || task.status === "queued" || task.status === "awaiting_answer" || task.status === "awaiting_approval") && (
        <button
          style={{ marginTop: 6, color: "#ff6b6b" }}
          onClick={async () => {
            await killTask(task.id);
            onChange();
          }}
        >
          Kill
        </button>
      )}

      {task.children.length > 0 && (
        <div style={{ marginLeft: 16, marginTop: 8, borderLeft: "2px solid #333", paddingLeft: 8 }}>
          {task.children.map((child) => (
            <TaskCard key={child.id} task={child} onChange={onChange} />
          ))}
        </div>
      )}
    </div>
  );
}

function Section({ title, tasks, onChange }: { title: string; tasks: DashboardTask[]; onChange: () => void }) {
  return (
    <div style={{ flex: 1, minWidth: 260 }}>
      <h3 style={{ color: "#eee" }}>
        {title} ({tasks.length})
      </h3>
      {tasks.map((task) => (
        <TaskCard key={task.id} task={task} onChange={onChange} />
      ))}
    </div>
  );
}

export function Dashboard() {
  const [data, setData] = useState<DashboardResponse | null>(null);

  async function refresh() {
    const d = await getDashboard();
    setData(d);
  }

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, []);

  if (!data) {
    return (
      <div style={{ background: "#121212", color: "#eee", minHeight: "100vh", padding: 16 }}>
        Loading…
      </div>
    );
  }

  const { kill_switch, queued, running, needs_you, done } = data;

  return (
    <div style={{ background: "#121212", color: "#eee", minHeight: "100vh", padding: 16 }}>
      <div
        style={{
          display: "flex",
          gap: 24,
          alignItems: "center",
          padding: 12,
          background: "#1e1e1e",
          borderRadius: 6,
          marginBottom: 16,
          flexWrap: "wrap",
        }}
      >
        <strong>Command Center</strong>
        <span>Queued: {queued.length}</span>
        <span>Running: {running.length}</span>
        <span>Needs you: {needs_you.length}</span>
        <span>Done: {done.length}</span>
        <span style={{ color: "#999" }}>
          Mode: {kill_switch.mode}
          {kill_switch.running !== undefined && kill_switch.cap !== undefined
            ? ` (${kill_switch.running}/${kill_switch.cap})`
            : ""}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button
            disabled={kill_switch.mode === "normal"}
            onClick={async () => {
              await setKillSwitch("normal");
              refresh();
            }}
          >
            Resume
          </button>
          <button
            disabled={kill_switch.mode === "pause"}
            onClick={async () => {
              await setKillSwitch("pause");
              refresh();
            }}
          >
            Pause
          </button>
          <button
            disabled={kill_switch.mode === "stop"}
            style={{ color: "#ff6b6b" }}
            onClick={async () => {
              await setKillSwitch("stop");
              refresh();
            }}
          >
            Stop
          </button>
        </div>
      </div>

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <Section title="Needs you" tasks={needs_you} onChange={refresh} />
        <Section title="Running" tasks={running} onChange={refresh} />
        <Section title="Queued" tasks={queued} onChange={refresh} />
      </div>

      <details style={{ marginTop: 16 }}>
        <summary style={{ cursor: "pointer", color: "#999" }}>Done ({done.length})</summary>
        <ul style={{ color: "#777" }}>
          {done.map((task) => (
            <li key={task.id}>
              #{task.id} · {task.description} — {task.status}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
