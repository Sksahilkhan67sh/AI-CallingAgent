"use client";

import { useEffect, useState } from "react";
import { adminApi } from "@/lib/admin-api";
import { useAuth } from "@/lib/auth-context";
import styles from "./TopBar.module.css";
import { StatusDot } from "./StatusDot";

export function TopBar({ title }: { title: string }) {
  const { session, logout } = useAuth();
  const [systemOk, setSystemOk] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    adminApi
      .systemHealth()
      .then((health) => {
        if (cancelled) return;
        setSystemOk(health.components.every((c) => c.status !== "degraded"));
      })
      .catch(() => {
        if (!cancelled) setSystemOk(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <header className={styles.topBar}>
      <h1 className={styles.title}>{title}</h1>
      <div className={styles.right}>
        {systemOk !== null && (
          <StatusDot label={systemOk ? "All systems normal" : "Degraded"} />
        )}
        {session && (
          <span className={styles.user}>
            {session.username} · {session.role}
          </span>
        )}
        <button type="button" className={styles.logout} onClick={logout}>
          Sign out
        </button>
      </div>
    </header>
  );
}
