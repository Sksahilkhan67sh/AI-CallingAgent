import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HealthStatus } from "@/components/health-status";

describe("HealthStatus", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the backend status once the health check resolves", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          status: "ok",
          service: "AI Calling Agent API",
          environment: "development",
        }),
      }),
    );

    render(<HealthStatus />);

    expect(screen.getByRole("status")).toHaveTextContent(
      "Checking backend health",
    );

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("ok"),
    );
  });

  it("shows an error when the backend is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503 }),
    );

    render(<HealthStatus />);

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Backend unreachable",
      ),
    );
  });
});
