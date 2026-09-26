"use client";

import { useCallback, useState } from "react";
import { useParams } from "next/navigation";
import { adminApi, ApiError } from "@/lib/admin-api";
import { useAdminQuery } from "@/lib/use-admin-query";
import { useAuth } from "@/lib/auth-context";
import { PageBody } from "@/components/admin/PageBody";
import { Section } from "@/components/admin/Section";
import { MetricRow } from "@/components/admin/MetricRow";
import { StatusDot } from "@/components/admin/StatusDot";
import { ErrorState } from "@/components/admin/States";
import type { CampaignStatus } from "@/lib/admin-types";
import styles from "./page.module.css";

// Checkpoint 07 §12: only transitions the backend's own state machine
// already allows are offered -- this list mirrors
// CampaignService.update_campaign's ALLOWED_TRANSITIONS, but the
// backend is still what enforces it; a stale/incorrect frontend list
// here can only produce a rejected request, never an invalid state.
const TRANSITIONS: Record<CampaignStatus, CampaignStatus[]> = {
  draft: ["active"],
  active: ["paused", "completed"],
  paused: ["active", "completed"],
  completed: [],
};

export default function CampaignDetailPage() {
  const params = useParams<{ id: string }>();
  const { session } = useAuth();
  const [actionError, setActionError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const fetcher = useCallback(() => adminApi.getCampaign(params.id), [params.id]);
  const query = useAdminQuery(fetcher, [params.id]);

  async function transition(next: CampaignStatus) {
    setActionError(null);
    setSubmitting(true);
    try {
      await adminApi.setCampaignStatus(params.id, next);
      query.refetch();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Unable to update campaign.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <PageBody title="Campaign">
      {query.status === "loading" && <div className={styles.loading}>Loading campaign…</div>}
      {query.status === "error" && <ErrorState message={query.message} onRetry={query.refetch} />}
      {query.status === "success" && (
        <>
          <Section
            title={query.data.name}
            action={
              session?.role === "admin" && (
                <div className={styles.actions}>
                  {TRANSITIONS[query.data.status].map((next) => (
                    <button
                      key={next}
                      type="button"
                      className={styles.actionButton}
                      disabled={submitting}
                      onClick={() => transition(next)}
                    >
                      {next === "active" ? "Activate" : next[0].toUpperCase() + next.slice(1)}
                    </button>
                  ))}
                </div>
              )
            }
          >
            <StatusDot label={query.data.status} />
            {actionError && <p className={styles.actionError}>{actionError}</p>}
          </Section>

          <Section title="Metrics">
            <MetricRow
              metrics={[
                { label: "Contacts", value: String(query.data.metrics.contacts) },
                { label: "Attempts", value: String(query.data.metrics.attempts) },
                {
                  label: "Completed",
                  value: String(query.data.metrics.completed),
                  tone: "success",
                },
                {
                  label: "Completed (partial)",
                  value: String(query.data.metrics.completed_partial),
                },
                {
                  label: "Active",
                  value: String(query.data.metrics.active),
                  tone: "warning",
                },
                { label: "Retry scheduled", value: String(query.data.metrics.retry_scheduled) },
                {
                  label: "Interested",
                  value: String(query.data.metrics.interested),
                  tone: "success",
                },
                {
                  label: "Avg. lead score",
                  value:
                    query.data.metrics.average_lead_score === null
                      ? "—"
                      : query.data.metrics.average_lead_score.toFixed(0),
                },
              ]}
            />
          </Section>
        </>
      )}
    </PageBody>
  );
}
