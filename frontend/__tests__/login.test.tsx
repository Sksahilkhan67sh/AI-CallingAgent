import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const pushMock = vi.fn();
const replaceMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  usePathname: () => "/login",
}));

import LoginPage from "@/app/login/page";
import { AuthProvider } from "@/lib/auth-context";

describe("LoginPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    pushMock.mockClear();
    window.sessionStorage.clear();
  });

  it("signs in and redirects to /admin on success (§43 auth flow)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          access_token: "tok123",
          token_type: "bearer",
          expires_in: 3600,
          role: "admin",
          username: "admin",
        }),
      })
    );

    render(
      <AuthProvider>
        <LoginPage />
      </AuthProvider>
    );

    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/admin"));
    expect(window.sessionStorage.getItem("admin_dashboard_session")).toContain("tok123");
  });

  it("shows an error message and never redirects on invalid credentials (§27, §47)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        json: async () => ({ detail: "Invalid username or password" }),
      })
    );

    render(
      <AuthProvider>
        <LoginPage />
      </AuthProvider>
    );

    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(pushMock).not.toHaveBeenCalled();
    // never a raw backend/network error string
    expect(screen.queryByText(/traceback|stack|sqlalchemy/i)).not.toBeInTheDocument();
  });
});
