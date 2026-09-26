"use client";

import { useCallback, useEffect } from "react";
import { adminApi } from "@/lib/admin-api";
import { useAdminQuery } from "@/lib/use-admin-query";
import { PageBody } from "@/components/admin/PageBody";
import { Section } from "@/components/admin/Section";
import { StatusDot } from "@/components/admin/StatusDot";
import { ErrorState } from "@/components/admin/States";
import tableStyles from "@/components/admin/table.module.css";
import styles from "./page.module.css";

/**
 * §39: system health is polled, not real-time -- no WebSocket/SSE
 * infrastructure exists in this repository yet (checked: no
 * socket.io/ws dependency, no such route anywhere in the backend). A
 * 15s interval is a reasonable, documented default for an operator
 * glancing at this page; see docs/CHECKPOINT-07-NOTES.md.
 */
const POLL_INTERVAL_MS = 15_000;

export default function SystemPage() {
  const fetcher = useCallback(() => adminApi.systemHealth(), []);
  const query = useAdminQuery(fetcher, []);

  useEffect(() => {
    const id = setInterval(() => query.refetch(), POLL_INTERVAL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <PageBody title="System">
      {query.status === "loading" && <div className={styles.loading}>Loading system status…</div>}
      {query.status === "error" && <ErrorState message={query.message} onRetry={query.refetch} />}
      {query.status === "success" && (
        <>
          <Section title="Components">
            <div className={tableStyles.tableWrap}>
              <table className={tableStyles.table}>
                <thead>
                  <tr>
                    <th>Component</th>
                    <th>Status</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {query.data.components.map((c) => (
                    <tr key={c.name}>
                      <td>{c.name}</td>
                      <td>
                        <StatusDot
                          label={c.status}
                          tone={
                            c.status === "ok"
                              ? "success"
                              : c.status === "degraded"
                                ? "danger"
                                : "neutral"
                          }
                        />
                      </td>
                      <td className={tableStyles.muted}>{c.detail ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <Section title="Queues">
            <div className={tableStyles.tableWrap}>
              <table className={tableStyles.table}>
                <thead>
                  <tr>
                    <th>Queue</th>
                    <th className={tableStyles.numeric}>Pending</th>
                    <th className={tableStyles.numeric}>Oldest pending</th>
                  </tr>
                </thead>
                <tbody>
                  {query.data.queues.map((q) => (
                    <tr key={q.name}>
                      <td>{q.name}</td>
                      <td className={tableStyles.numeric}>{q.pending}</td>
                      <td className={tableStyles.numeric}>
                        {q.oldest_pending_seconds === null
                          ? "—"
                          : `${Math.round(q.oldest_pending_seconds)}s`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        </>
      )}
    </PageBody>
  );
}
