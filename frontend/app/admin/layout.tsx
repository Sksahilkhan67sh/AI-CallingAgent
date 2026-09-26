"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { Sidebar } from "@/components/admin/Sidebar";
import styles from "./layout.module.css";

/**
 * Checkpoint 07 §4: this client-side redirect is a convenience, not
 * the security boundary -- every admin API call is independently
 * authorized server-side (see app/api/admin_deps.py in the backend).
 * A user who bypasses this check simply gets 401s from every request.
 */
function AuthGate({ children }: { children: React.ReactNode }) {
  const { session, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !session) {
      router.replace("/login");
    }
  }, [loading, session, router]);

  if (loading) return null;
  if (!session) return null;

  return <>{children}</>;
}

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGate>
      <div className={styles.shell}>
        <Sidebar />
        <main className={styles.content}>{children}</main>
      </div>
    </AuthGate>
  );
}
