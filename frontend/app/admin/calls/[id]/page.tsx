"use client";

import { useCallback } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { adminApi } from "@/lib/admin-api";
import { useAdminQuery } from "@/lib/use-admin-query";
import { PageBody } from "@/components/admin/PageBody";
import { Section } from "@/components/admin/Section";
import { StatusDot } from "@/components/admin/StatusDot";
import { TranscriptViewer } from "@/components/admin/TranscriptViewer";
import { AnalysisPanel } from "@/components/admin/AnalysisPanel";
import { RecoveryTimeline } from "@/components/admin/RecoveryTimeline";
import { ErrorState } from "@/components/admin/States";
import styles from "./page.module.css";

export default function CallDetailPage() {
  const params = useParams<{ id: string }>();
  const fetcher = useCallback(() => adminApi.getCallAttempt(params.id), [params.id]);
  const query = useAdminQuery(fetcher, [params.id]);

  return (
    <PageBody title="Call">
      {query.status === "loading" && <div className={styles.loading}>Loading call…</div>}
      {query.status === "error" && <ErrorState message={query.message} onRetry={query.refetch} />}
      {query.status === "success" && (
        <>
          <Section title={`${query.data.contact_phone_masked} · Attempt ${query.data.attempt_number}`}>
            <dl className={styles.detailGrid}>
              <div>
                <dt>Outcome</dt>
                <dd>
                  <StatusDot
                    label={
                      query.data.disconnect_reason ??
                      query.data.connection_failure_reason ??
                      query.data.state
                    }
                  />
                </dd>
              </div>
              <div>
                <dt>Campaign</dt>
                <dd>
                  <Link href={`/admin/campaigns/${query.data.campaign_id}`}>
                    {query.data.campaign_name}
                  </Link>
                </dd>
              </div>
              <div>
                <dt>Contact</dt>
                <dd>
                  <Link href={`/admin/contacts/${query.data.contact_id}`}>
                    {query.data.contact_phone_masked}
                  </Link>
                </dd>
              </div>
              <div>
                <dt>Provider</dt>
                <dd>{query.data.provider ?? "—"}</dd>
              </div>
              <div>
                <dt>Started</dt>
                <dd>{new Date(query.data.started_at).toLocaleString()}</dd>
              </div>
              <div>
                <dt>Ended</dt>
                <dd>{query.data.ended_at ? new Date(query.data.ended_at).toLocaleString() : "—"}</dd>
              </div>
            </dl>
          </Section>

          <Section title="Recovery History">
            <RecoveryTimeline events={query.data.recovery_events} />
          </Section>

          <Section title="Transcript">
            <TranscriptViewer lines={query.data.transcript} />
          </Section>

          <Section title="AI Intelligence">
            <AnalysisPanel analysis={query.data.analysis} />
          </Section>
        </>
      )}
    </PageBody>
  );
}
