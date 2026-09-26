"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  adminApi,
  clearStoredToken,
  getStoredSession,
  storeSession,
  type StoredSession,
} from "./admin-api";

interface AuthContextValue {
  session: StoredSession | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<StoredSession | null>(null);
  const [loading, setLoading] = useState(true);
  const router = useRouter();

  useEffect(() => {
    setSession(getStoredSession());
    setLoading(false);
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const result = await adminApi.login(username, password);
    storeSession(result);
    setSession(getStoredSession());
  }, []);

  const logout = useCallback(() => {
    clearStoredToken();
    setSession(null);
    router.push("/login");
  }, [router]);

  const value = useMemo(
    () => ({ session, loading, login, logout }),
    [session, loading, login, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
