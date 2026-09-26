"use client";

import { useCallback } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { adminApi } from "@/lib/admin-api";
import { useAdminQuery } from "@/lib/use-admin-query";
import { PageBody } from "@/components/admin/PageBody";
import { Section } from "@/components/admin/Section";
import { MetricRow } from "@/components/admin/MetricRow";
import { ErrorState } from "@/components/admin/States";
import filterStyles from "@/components/admin/Filters.module.css";
import styles from "./page.module.css";

const RANGE_OPTIONS: { value: string; label: string }[] = [
  { value: "today", label: "Today" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
];

function pct(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export default function AnalyticsPage() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const range = searchParams.get("range") ?? "7d";

  const fetcher = useCallback(() => adminApi.analytics(range), [range]);
  const query = useAdminQuery(fetcher, [range]);

  function updateRange(next: string) {
    const params = new URLSearchParams(searchParams.toString());
    params.set("range", next);
    router.push(`/admin/analytics?${params}`);
  }

  return (
    <PageBody title="Analytics">
      <div className={filterStyles.bar}>
        <select
          className={filterStyles.select}
          value={range}
          onChange={(e) => updateRange(e.target.value)}
          aria-label="Time range"
        >
          {RANGE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      {query.status === "loading" && <div className={styles.loading}>Loading analytics…</div>}
      {query.status === "error" && <ErrorState message={query.message} onRetry={query.refetch} />}
      {query.status === "success" && query.data.total_dial_attempts === 0 && (
        <p className={styles.noData}>No data for this period.</p>
      )}
      {query.status === "success" && query.data.total_dial_attempts > 0 && (
        <>
          <Section title="Call Volume">
            <MetricRow
              metrics={[
                { label: "Dial attempts", value: String(query.data.total_dial_attempts) },
                { label: "Connected", value: String(query.data.connected_attempts) },
                { label: "Connection rate", value: pct(query.data.connection_rate) },
                { label: "Completed", value: String(query.data.completed_attempts) },
                { label: "Completion rate", value: pct(query.data.completion_rate) },
                {
                  label: "Avg. duration",
                  value:
                    query.data.average_duration_seconds === null
                      ? "—"
                      : `${Math.round(query.data.average_duration_seconds)}s`,
                },
              ]}
            />
          </Section>

          <Section title="Reliability">
            <MetricRow
              metrics={[
                {
                  label: "Attempts requiring recovery",
                  value: String(query.data.attempts_requiring_recovery),
                },
                { label: "Retry rate", value: pct(query.data.retry_rate) },
                { label: "Opt-outs", value: String(query.data.opt_out_count) },
                { label: "Opt-out rate", value: pct(query.data.opt_out_rate) },
              ]}
            />
          </Section>

          <Section title="AI Intelligence">
            <MetricRow
              metrics={[
                { label: "Analysis jobs", value: String(query.data.analysis_jobs_total) },
                { label: "Analyzed", value: String(query.data.analyzed_calls) },
                {
                  label: "Analysis completion rate",
                  value: pct(query.data.analysis_completion_rate),
                },
                { label: "Interested", value: String(query.data.interested_calls) },
                { label: "Interest rate", value: pct(query.data.interest_rate) },
                {
                  label: "Avg. lead score",
                  value:
                    query.data.average_lead_score === null
                      ? "—"
                      : query.data.average_lead_score.toFixed(0),
                },
              ]}
            />
          </Section>
        </>
      )}
    </PageBody>
  );
}
