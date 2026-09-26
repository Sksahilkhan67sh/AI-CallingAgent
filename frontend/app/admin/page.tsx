"use client";

import { useCallback } from "react";
import { adminApi } from "@/lib/admin-api";
import { useAdminQuery } from "@/lib/use-admin-query";
import { PageBody } from "@/components/admin/PageBody";
import { Section } from "@/components/admin/Section";
import { MetricRow } from "@/components/admin/MetricRow";
import { ErrorState } from "@/components/admin/States";
import styles from "./page.module.css";

function pct(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export default function DashboardHomePage() {
  const fetcher = useCallback(() => adminApi.dashboardOverview(), []);
  const query = useAdminQuery(fetcher, []);

  return (
    <PageBody title="Overview">
      {query.status === "loading" && <div className={styles.loading}>Loading overview…</div>}
      {query.status === "error" && (
        <ErrorState message={query.message} onRetry={query.refetch} />
      )}
      {query.status === "success" && (
        <>
          <Section title="Call Operations">
            <MetricRow
              metrics={[
                { label: "Total contacts", value: String(query.data.calls.total) },
                { label: "Active", value: String(query.data.calls.active), tone: "warning" },
                {
                  label: "Completed",
                  value: String(query.data.calls.completed),
                  tone: "success",
                },
                {
                  label: "Completed (partial)",
                  value: String(query.data.calls.completed_partial),
                },
                { label: "Retry scheduled", value: String(query.data.calls.retry_scheduled) },
                { label: "Not yet dialed", value: String(query.data.calls.queued) },
                { label: "Closed", value: String(query.data.calls.closed) },
              ]}
            />
          </Section>

          <Section title="Campaign Performance">
            <MetricRow
              metrics={[
                {
                  label: "Active",
                  value: String(query.data.campaigns.active),
                  tone: "success",
                },
                { label: "Paused", value: String(query.data.campaigns.paused), tone: "warning" },
                { label: "Completed", value: String(query.data.campaigns.completed) },
                { label: "Draft", value: String(query.data.campaigns.draft) },
              ]}
            />
          </Section>

          <Section title="AI Intelligence">
            <MetricRow
              metrics={[
                { label: "Analyzed calls", value: String(query.data.intelligence.analyzed) },
                {
                  label: "Interested",
                  value: String(query.data.intelligence.interested),
                  tone: "success",
                },
                { label: "Maybe", value: String(query.data.intelligence.maybe), tone: "warning" },
                {
                  label: "Not interested",
                  value: String(query.data.intelligence.not_interested),
                  tone: "danger",
                },
                { label: "Unknown", value: String(query.data.intelligence.unknown) },
                {
                  label: "Avg. lead score",
                  value:
                    query.data.intelligence.average_lead_score === null
                      ? "—"
                      : query.data.intelligence.average_lead_score.toFixed(0),
                },
              ]}
            />
          </Section>

          <Section title="Reliability">
            <MetricRow
              metrics={[
                { label: "Retry rate", value: pct(query.data.reliability.retry_rate) },
                {
                  label: "Failure rate",
                  value: pct(query.data.reliability.failure_rate),
                  tone:
                    query.data.reliability.failure_rate !== null &&
                    query.data.reliability.failure_rate > 0.2
                      ? "danger"
                      : undefined,
                },
                {
                  label: "Analysis failure rate",
                  value: pct(query.data.reliability.analysis_failure_rate),
                },
                {
                  label: "Outbound queue depth",
                  value: String(query.data.reliability.outbound_queue_depth),
                },
                {
                  label: "Analysis queue depth",
                  value: String(query.data.reliability.analysis_queue_depth),
                },
              ]}
            />
          </Section>
        </>
      )}
    </PageBody>
  );
}
