// Centralized admin API client -- Checkpoint 07 §28. Every admin fetch
// goes through here: base URL, auth header, JSON parsing, and error
// mapping live in one place instead of being scattered across pages.

import type {
  AnalyticsResponse,
  CallAttemptDetail,
  CallAttemptListItem,
  CampaignDetail,
  CampaignListItem,
  CampaignStatus,
  ContactDetail,
  ContactListItem,
  DashboardOverview,
  LoginResponse,
  Page,
  SystemHealthResponse,
} from "./admin-types";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

// §36-37: the JWT is a bearer credential for a browser-only admin tool
// with no user-controlled content and nothing more sensitive reachable
// than what the token itself already grants; it is kept in memory +
// sessionStorage (cleared when the tab closes), never localStorage or
// a non-expiring cookie. This is a documented, intentional trade-off
// for this checkpoint's scope, not a full session-security design --
// see docs/CHECKPOINT-07-NOTES.md.
const TOKEN_STORAGE_KEY = "admin_dashboard_session";

export interface StoredSession {
  token: string;
  username: string;
  role: "admin" | "operator";
  expiresAt: number; // epoch ms
}

export function getStoredSession(): StoredSession | null {
  if (typeof window === "undefined") return null;
  const raw = window.sessionStorage.getItem(TOKEN_STORAGE_KEY);
  if (!raw) return null;
  try {
    const session = JSON.parse(raw) as StoredSession;
    if (session.expiresAt < Date.now()) {
      window.sessionStorage.removeItem(TOKEN_STORAGE_KEY);
      return null;
    }
    return session;
  } catch {
    return null;
  }
}

export function getStoredToken(): string | null {
  return getStoredSession()?.token ?? null;
}

export function storeSession(login: LoginResponse): void {
  const session: StoredSession = {
    token: login.access_token,
    username: login.username,
    role: login.role,
    expiresAt: Date.now() + login.expires_in * 1000,
  };
  window.sessionStorage.setItem(TOKEN_STORAGE_KEY, JSON.stringify(session));
}

export function clearStoredToken(): void {
  window.sessionStorage.removeItem(TOKEN_STORAGE_KEY);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getStoredToken();
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (init?.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "Unable to reach the server. Check your connection and try again.");
  }

  if (response.status === 401) {
    clearStoredToken();
    throw new ApiError(401, "Your session has expired. Please sign in again.");
  }
  if (response.status === 403) {
    throw new ApiError(403, "You don't have permission to do that.");
  }
  if (response.status === 404) {
    throw new ApiError(404, "Not found.");
  }
  if (!response.ok) {
    // §27, §47: never surface raw backend exception text to the UI.
    throw new ApiError(response.status, "Something went wrong. Please try again.");
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const adminApi = {
  login: (username: string, password: string) =>
    request<LoginResponse>("/api/v1/admin/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),

  dashboardOverview: () => request<DashboardOverview>("/api/v1/admin/dashboard/overview"),

  analytics: (range: string, campaignId?: string) => {
    const params = new URLSearchParams({ range });
    if (campaignId) params.set("campaign_id", campaignId);
    return request<AnalyticsResponse>(`/api/v1/admin/dashboard/analytics?${params}`);
  },

  systemHealth: () => request<SystemHealthResponse>("/api/v1/admin/dashboard/system"),

  listCampaigns: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    q.set("limit", String(params.limit ?? 50));
    q.set("offset", String(params.offset ?? 0));
    return request<Page<CampaignListItem>>(`/api/v1/admin/campaigns?${q}`);
  },

  getCampaign: (id: string) => request<CampaignDetail>(`/api/v1/admin/campaigns/${id}`),

  setCampaignStatus: (id: string, status: CampaignStatus) =>
    request(`/api/v1/admin/campaigns/${id}/status?new_status=${status}`, { method: "POST" }),

  listContacts: (
    params: { campaignId?: string; status?: string; limit?: number; offset?: number } = {}
  ) => {
    const q = new URLSearchParams();
    if (params.campaignId) q.set("campaign_id", params.campaignId);
    if (params.status) q.set("status", params.status);
    q.set("limit", String(params.limit ?? 50));
    q.set("offset", String(params.offset ?? 0));
    return request<Page<ContactListItem>>(`/api/v1/admin/contacts?${q}`);
  },

  getContact: (id: string) => request<ContactDetail>(`/api/v1/admin/contacts/${id}`),

  listCallAttempts: (
    params: {
      campaignId?: string;
      contactId?: string;
      state?: string;
      analysisStatus?: string;
      limit?: number;
      offset?: number;
    } = {}
  ) => {
    const q = new URLSearchParams();
    if (params.campaignId) q.set("campaign_id", params.campaignId);
    if (params.contactId) q.set("contact_id", params.contactId);
    if (params.state) q.set("state", params.state);
    if (params.analysisStatus) q.set("analysis_status", params.analysisStatus);
    q.set("limit", String(params.limit ?? 50));
    q.set("offset", String(params.offset ?? 0));
    return request<Page<CallAttemptListItem>>(`/api/v1/admin/call-attempts?${q}`);
  },

  getCallAttempt: (id: string) =>
    request<CallAttemptDetail>(`/api/v1/admin/call-attempts/${id}`),
};
