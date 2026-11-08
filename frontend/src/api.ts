// Все запросы идут на тот же origin (nginx фронтенда проксирует /auth и /api в bionicpro-auth).
// Токенов во фронтенде нет: браузер автоматически отправляет HttpOnly-cookie сессии.

export interface UserProfile {
  sub: string;
  username: string;
  email: string | null;
  name: string | null;
  roles: string[];
  access_token_expires_in: number;
}

export interface ReportLink {
  user: string;
  period: { from: string; to: string };
  available_until: string;
  truncated: boolean;
  data_version: string;
  cache: 'hit' | 'miss';
  url: string;
  url_expires_in: number;
}

export interface DailyRow {
  report_date: string;
  prosthesis_serial: string;
  prosthesis_model: string;
  events_count: number;
  movements_count: number;
  errors_count: number;
  slow_reactions_count: number;
  avg_reaction_ms: number;
  p95_reaction_ms: number;
  max_reaction_ms: number;
  avg_signal_amplitude: number;
  min_battery_level: number;
  avg_battery_level: number;
}

export interface SummaryRow {
  prosthesis_serial: string;
  prosthesis_model: string;
  days: number;
  events_count: number;
  movements_count: number;
  errors_count: number;
  slow_reactions_count: number;
  slow_reactions_share: number;
  avg_reaction_ms: number;
  max_reaction_ms: number;
  min_battery_level: number;
}

export interface Report {
  customer: { customer_id: number; email: string; full_name: string; country: string };
  period: { from: string; to: string };
  data_version: string;
  source: string;
  generated_at: string;
  summary: SummaryRow[];
  daily: DailyRow[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function errorMessage(resp: Response): Promise<string> {
  try {
    const body = await resp.json();
    const detail = body.detail ?? body;
    if (typeof detail === 'string') return detail;
    if (detail.message) {
      return detail.available_until
        ? `${detail.message} (данные доступны до ${detail.available_until})`
        : detail.message;
    }
    return JSON.stringify(detail);
  } catch {
    return resp.statusText;
  }
}

export async function fetchMe(): Promise<UserProfile | null> {
  const resp = await fetch('/auth/me', { credentials: 'same-origin' });
  if (resp.status === 401) return null;
  if (!resp.ok) throw new ApiError(resp.status, await errorMessage(resp));
  return resp.json();
}

export function login(idp?: string): void {
  const params = new URLSearchParams({ return_to: '/' });
  if (idp) params.set('idp', idp);
  window.location.assign(`/auth/login?${params.toString()}`);
}

export async function logout(): Promise<void> {
  await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
}

export async function requestReport(dateFrom?: string, dateTo?: string): Promise<ReportLink> {
  const params = new URLSearchParams();
  if (dateFrom) params.set('date_from', dateFrom);
  if (dateTo) params.set('date_to', dateTo);
  const resp = await fetch(`/api/reports?${params.toString()}`, { credentials: 'same-origin' });
  if (!resp.ok) throw new ApiError(resp.status, await errorMessage(resp));
  return resp.json();
}

export async function downloadReport(link: ReportLink): Promise<Report> {
  // Готовый отчет отдается через CDN (nginx + Minio) по подписанной ссылке
  const resp = await fetch(link.url, { credentials: 'omit' });
  if (!resp.ok) throw new ApiError(resp.status, `CDN: ${resp.statusText}`);
  return resp.json();
}
