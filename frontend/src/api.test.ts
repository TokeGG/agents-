import { describe, expect, it, vi } from "vitest";
import { createTask, getDashboard, setKillSwitch, killTask } from "./api";

describe("getDashboard", () => {
  it("GETs /dashboard", async () => {
    vi.stubEnv("VITE_AUTH_TOKEN", "test-token");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ queued: [], running: [], needs_you: [], done: [], kill_switch: { mode: "normal" } }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await getDashboard();
    expect(fetchMock).toHaveBeenCalledWith(
      "/dashboard",
      expect.objectContaining({ headers: expect.objectContaining({ Authorization: expect.stringMatching(/^Bearer .+/) }) })
    );
  });
});

describe("setKillSwitch", () => {
  it("POSTs the mode to /kill-switch", async () => {
    vi.stubEnv("VITE_AUTH_TOKEN", "test-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ mode: "pause" }) });
    vi.stubGlobal("fetch", fetchMock);
    const result = await setKillSwitch("pause");
    expect(fetchMock).toHaveBeenCalledWith(
      "/kill-switch",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          Authorization: expect.stringMatching(/^Bearer .+/),
        }),
        body: JSON.stringify({ mode: "pause" }),
      })
    );
    expect(result).toEqual({ mode: "pause" });
  });
});

describe("killTask", () => {
  it("POSTs to /tasks/{id}/kill", async () => {
    vi.stubEnv("VITE_AUTH_TOKEN", "test-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 5, status: "killed" }) });
    vi.stubGlobal("fetch", fetchMock);
    const result = await killTask(5);
    expect(fetchMock).toHaveBeenCalledWith(
      "/tasks/5/kill",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          Authorization: expect.stringMatching(/^Bearer .+/),
        }),
      })
    );
    expect(result).toEqual({ id: 5, status: "killed" });
  });
});

describe("createTask", () => {
  it("POSTs the description and fullAuto flag to /tasks", async () => {
    vi.stubEnv("VITE_AUTH_TOKEN", "test-token");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: 1, status: "running" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await createTask("say hello", true);

    expect(fetchMock).toHaveBeenCalledWith(
      "/tasks",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          Authorization: expect.stringMatching(/^Bearer .+/),
        }),
        body: JSON.stringify({ description: "say hello", full_auto: true }),
      })
    );
    expect(result).toEqual({ id: 1, status: "running" });
  });
});
