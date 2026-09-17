import { HealthStatus } from "@/components/health-status";

export default function Home() {
  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "3rem" }}>
      <h1>AI Calling Agent</h1>
      <p>Checkpoint 00 — project foundation.</p>
      <HealthStatus />
    </main>
  );
}
