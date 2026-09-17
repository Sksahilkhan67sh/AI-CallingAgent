"use client";

import { useEffect, useState } from "react";

import { fetchHealth, type HealthResponse } from "@/lib/api";

type State =
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "ready"; data: HealthResponse };

export function HealthStatus() {
  const [state, setState] = useState<State>({ phase: "loading" });

  useEffect(() => {
    let cancelled = false;

    fetchHealth()
      .then((data) => {
        if (!cancelled) setState({ phase: "ready", data });
      })
      .catch((err: Error) => {
        if (!cancelled) setState({ phase: "error", message: err.message });
      });

    return () => {
      cancelled = true;
    };
  }, []);

  if (state.phase === "loading") {
    return <p role="status">Checking backend health…</p>;
  }

  if (state.phase === "error") {
    return (
      <p role="alert" style={{ color: "crimson" }}>
        Backend unreachable: {state.message}
      </p>
    );
  }

  return (
    <p role="status">
      Backend status: <strong>{state.data.status}</strong> (
      {state.data.service}, {state.data.environment})
    </p>
  );
}
