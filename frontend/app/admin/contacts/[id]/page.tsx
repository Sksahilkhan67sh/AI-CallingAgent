"use client";

import { useCallback } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { adminApi } from "@/lib/admin-api";
import { useAdminQuery } from "@/lib/use-admin-query";
import { PageBody } from "@/components/admin/PageBody";
import { Section } from "@/components/admin/Section";
import { StatusDot } from "@/components/admin/StatusDot";
import { ErrorState, EmptyState, TableSkeleton } from "@/components/admin/States";
import tableStyles from "@/components/admin/table.module.css";
import styles from "./page.module.css";

export default function ContactDetailPage() {
  const params = useParams<{ id: string }>();

  const contactFetcher = useCallback(() => adminApi.getContact(params.id), [params.id]);
  const contactQuery = useAdminQuery(contactFetcher, [params.id]);

  const attemptsFetcher = useCallback(
    () => adminApi.listCallAttempts({ contactId: params.id, limit: 50, offset: 0 }),
    [params.id]
  );
  const attemptsQuery = useAdminQuery(attemptsFetcher, [params.id]);

  return (
    <PageBody title="Contact">
      {contactQuery.status === "loading" && <div className={styles.loading}>Loading contact…</div>}
      {contactQuery.status === "error" && (
        <ErrorState message={contactQuery.message} onRetry={contactQuery.refetch} />
      )}
      {contactQuery.status === "success" && (
        <>
          <Section title={contactQuery.data.phone_masked}>
            <dl className={styles.detailGrid}>
              <div>
                <dt>Status</dt>
                <dd>
                  <StatusDot label={contactQuery.data.status} />
                </dd>
              </div>
              <div>
                <dt>Campaign</dt>
                <dd>
                  <Link href={`/admin/campaigns/${contactQuery.data.campaign_id}`}>
                    {contactQuery.data.campaign_name}
                  </Link>
                </dd>
              </div>
              <div>
                <dt>Attempts</dt>
                <dd>{contactQuery.data.attempt_count}</dd>
              </div>
              <div>
                <dt>Suppressed</dt>
                <dd>
                  {contactQuery.data.suppressed ? (
                    <StatusDot label={contactQuery.data.suppression_reason ?? "Yes"} tone="danger" />
                  ) : (
                    "No"
                  )}
                </dd>
              </div>
              <div>
                <dt>Added</dt>
                <dd>{new Date(contactQuery.data.created_at).toLocaleString()}</dd>
              </div>
            </dl>
          </Section>

          <Section title="Call History">
            {attemptsQuery.status === "loading" && <TableSkeleton rows={3} />}
            {attemptsQuery.status === "error" && (
              <ErrorState message={attemptsQuery.message} onRetry={attemptsQuery.refetch} />
            )}
            {attemptsQuery.status === "success" && attemptsQuery.data.items.length === 0 && (
              <EmptyState title="No call attempts yet" />
            )}
            {attemptsQuery.status === "success" && attemptsQuery.data.items.length > 0 && (
              <div className={tableStyles.tableWrap}>
                <table className={tableStyles.table}>
                  <thead>
                    <tr>
                      <th>Attempt</th>
                      <th>Outcome</th>
                      <th>Started</th>
                      <th>Ended</th>
                      <th className={tableStyles.numeric}>Lead score</th>
                    </tr>
                  </thead>
                  <tbody>
                    {attemptsQuery.data.items
                      .sort((a, b) => a.attempt_number - b.attempt_number)
                      .map((a) => (
                        <tr key={a.id}>
                          <td>
                            <Link
                              href={`/admin/calls/${a.id}`}
                              className={tableStyles.rowLink}
                            >
                              #{a.attempt_number}
                            </Link>
                          </td>
                          <td>
                            <StatusDot label={a.disconnect_reason ?? a.state} />
                          </td>
                          <td className={tableStyles.muted}>
                            {new Date(a.started_at).toLocaleString()}
                          </td>
                          <td className={tableStyles.muted}>
                            {a.ended_at ? new Date(a.ended_at).toLocaleString() : "—"}
                          </td>
                          <td className={tableStyles.numeric}>{a.lead_score ?? "—"}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            )}
          </Section>
        </>
      )}
    </PageBody>
  );
}
