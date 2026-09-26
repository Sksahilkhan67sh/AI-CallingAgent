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
const STATUS_OPTIONS = ["", "draft", "active", "paused", "completed"];

export default function CampaignsPage() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const offset = Number(searchParams.get("offset") ?? 0);
  const status = searchParams.get("status") ?? "";

  const fetcher = useCallback(
    () => adminApi.listCampaigns({ status: status || undefined, limit: LIMIT, offset }),
    [status, offset]
  );
  const query = useAdminQuery(fetcher, [status, offset]);

  function updateParams(next: { offset?: number; status?: string }) {
    const params = new URLSearchParams(searchParams.toString());
    if (next.status !== undefined) {
      if (next.status) params.set("status", next.status);
      else params.delete("status");
      params.set("offset", "0");
    }
    if (next.offset !== undefined) params.set("offset", String(next.offset));
    router.push(`/admin/campaigns?${params}`);
  }

  return (
    <PageBody title="Campaigns">
      <div className={filterStyles.bar}>
        <select
          className={filterStyles.select}
          value={status}
          onChange={(e) => updateParams({ status: e.target.value })}
          aria-label="Filter by status"
        >
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt} value={opt}>
              {opt === "" ? "All statuses" : opt}
            </option>
          ))}
        </select>
      </div>

      {query.status === "loading" && <TableSkeleton />}
      {query.status === "error" && <ErrorState message={query.message} onRetry={query.refetch} />}
      {query.status === "success" && query.data.items.length === 0 && (
        <EmptyState title="No campaigns found" hint="Try adjusting the selected filter." />
      )}
      {query.status === "success" && query.data.items.length > 0 && (
        <>
          <div className={tableStyles.tableWrap}>
            <table className={tableStyles.table}>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Status</th>
                  <th className={tableStyles.numeric}>Contacts</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {query.data.items.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <Link href={`/admin/campaigns/${c.id}`} className={tableStyles.rowLink}>
                        {c.name}
                      </Link>
                    </td>
                    <td>
                      <StatusDot label={c.status} />
                    </td>
                    <td className={tableStyles.numeric}>{c.contact_count}</td>
                    <td className={tableStyles.muted}>
                      {new Date(c.created_at).toLocaleDateString()}
                    </td>
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
