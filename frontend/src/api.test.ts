import { describe, expect, it, vi } from "vitest";
import { createTask } from "./api";

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
