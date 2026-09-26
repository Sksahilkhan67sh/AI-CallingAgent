"use client";

import { useCallback } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { adminApi } from "@/lib/admin-api";
import { useAdminQuery } from "@/lib/use-admin-query";
import { PageBody } from "@/components/admin/PageBody";
import { Pagination } from "@/components/admin/Pagination";
import { StatusDot } from "@/components/admin/StatusDot";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/admin/States";
import tableStyles from "@/components/admin/table.module.css";
import filterStyles from "@/components/admin/Filters.module.css";

const LIMIT = 25;
const STATE_OPTIONS = [
  "",
  "Initiated",
  "Connected",
  "FailedToConnect",
  "DroppedMidCall",
  "EndedNormally",
];
const ANALYSIS_OPTIONS = ["", "pending", "processing", "completed", "failed"];

function formatDuration(startedAt: string, endedAt: string | null): string {
  if (!endedAt) return "—";
  const seconds = Math.max(
    0,
    Math.round((new Date(endedAt).getTime() - new Date(startedAt).getTime()) / 1000)
  );
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function CallsPage() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const offset = Number(searchParams.get("offset") ?? 0);
  const state = searchParams.get("state") ?? "";
  const analysisStatus = searchParams.get("analysis_status") ?? "";

  const fetcher = useCallback(
    () =>
      adminApi.listCallAttempts({
        state: state || undefined,
        analysisStatus: analysisStatus || undefined,
        limit: LIMIT,
        offset,
      }),
    [state, analysisStatus, offset]
  );
  const query = useAdminQuery(fetcher, [state, analysisStatus, offset]);

  function updateParams(next: { offset?: number; state?: string; analysis_status?: string }) {
    const params = new URLSearchParams(searchParams.toString());
    for (const key of ["state", "analysis_status"] as const) {
      if (next[key] !== undefined) {
        if (next[key]) params.set(key, next[key]!);
        else params.delete(key);
        params.set("offset", "0");
      }
    }
    if (next.offset !== undefined) params.set("offset", String(next.offset));
    router.push(`/admin/calls?${params}`);
  }

  return (
    <PageBody title="Calls">
      <div className={filterStyles.bar}>
        <select
          className={filterStyles.select}
          value={state}
          onChange={(e) => updateParams({ state: e.target.value })}
          aria-label="Filter by call state"
        >
          {STATE_OPTIONS.map((opt) => (
            <option key={opt} value={opt}>
              {opt === "" ? "All states" : opt}
            </option>
          ))}
        </select>
        <select
          className={filterStyles.select}
          value={analysisStatus}
          onChange={(e) => updateParams({ analysis_status: e.target.value })}
          aria-label="Filter by analysis status"
        >
          {ANALYSIS_OPTIONS.map((opt) => (
            <option key={opt} value={opt}>
              {opt === "" ? "All analysis statuses" : opt}
            </option>
          ))}
        </select>
      </div>

      {query.status === "loading" && <TableSkeleton columns={7} />}
      {query.status === "error" && <ErrorState message={query.message} onRetry={query.refetch} />}
      {query.status === "success" && query.data.items.length === 0 && (
        <EmptyState title="No calls found" hint="Try adjusting the selected filters." />
      )}
      {query.status === "success" && query.data.items.length > 0 && (
        <>
          <div className={tableStyles.tableWrap}>
            <table className={tableStyles.table}>
              <thead>
                <tr>
                  <th>Contact</th>
                  <th>Campaign</th>
                  <th className={tableStyles.numeric}>Attempt</th>
                  <th>Outcome</th>
                  <th>Duration</th>
                  <th>Started</th>
                  <th>Analysis</th>
                  <th className={tableStyles.numeric}>Lead score</th>
                </tr>
              </thead>
              <tbody>
                {query.data.items.map((a) => (
                  <tr key={a.id}>
                    <td>
                      <Link
                        href={`/admin/calls/${a.id}`}
                        className={`${tableStyles.rowLink} ${tableStyles.mono}`}
                      >
                        {a.contact_phone_masked}
                      </Link>
                    </td>
                    <td className={tableStyles.muted}>{a.campaign_name}</td>
                    <td className={tableStyles.numeric}>{a.attempt_number}</td>
                    <td>
                      <StatusDot label={a.disconnect_reason ?? a.state} />
                    </td>
                    <td className={tableStyles.muted}>
                      {formatDuration(a.started_at, a.ended_at)}
                    </td>
                    <td className={tableStyles.muted}>
                      {new Date(a.started_at).toLocaleString()}
                    </td>
                    <td>
                      {a.analysis_status ? <StatusDot label={a.analysis_status} /> : "—"}
                    </td>
                    <td className={tableStyles.numeric}>{a.lead_score ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination
            total={query.data.total}
            limit={LIMIT}
            offset={offset}
            onChange={(next) => updateParams({ offset: next })}
          />
        </>
      )}
    </PageBody>
  );
}
