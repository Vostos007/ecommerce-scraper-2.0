'use client';

import { z } from 'zod';

import {
  proxyStatsSchema,
  summaryResponseSchema,
  masterWorkbookStatusSchema,
  mapStatusSchema,
  mapFileMetadataSchema,
  authUserSchema,
  loginSchema
} from './validations';
import type { SiteSummary } from './sites';

const JSON_HEADERS = {
  'Content-Type': 'application/json'
};

export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly payload?: unknown) {
    super(message);
    this.name = 'ApiError';
  }
}

export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NetworkError';
  }
}

export interface LoginCredentials {
  username: string;
  password: string;
}

export type AuthUser = z.infer<typeof authUserSchema>;

export interface ExportConfig {
  concurrency?: number;
  resume?: boolean;
  limit?: number;
  args?: string[];
}

export type ExportJob = z.infer<typeof exportJobSchema>;

export type UploadResult = z.infer<typeof uploadResultSchema>;
export type ExportStatus = z.infer<typeof exportStatusSchema>;
export type MasterWorkbookStatus = z.infer<typeof masterWorkbookStatusSchema>;
export type SiteMapResponse = z.infer<typeof siteMapResponseSchema>;

export type QueuedExport = z.infer<typeof queuedExportSchema>;

export type ActiveExportJob = ExportStatus;
export type { SiteSummary };

export interface DownloadProgress {
  loaded: number;
  total?: number;
}

export interface DownloadExportOptions {
  format?: 'xlsx' | 'csv';
  sheet?: 'full' | 'seo' | 'diff';
}

const exportJobSchema = z.object({
  jobId: z.string(),
  site: z.string(),
  script: z.string(),
  python: z.string(),
  args: z.array(z.string()),
  startedAt: z.string()
});

const uploadResultSchema = z.object({
  site: z.string(),
  filename: z.string(),
  bytes: z.number(),
  savedPath: z.string(),
  canonicalPath: z.string().optional()
});

const siteStatusSchema = z.enum(['ready', 'missing_export', 'unknown', 'missing_map']);

const siteSummarySchema = z
  .object({
    domain: z.string(),
    name: z.string(),
    lastExport: z.string().nullable(),
    status: siteStatusSchema,
    script: z.string(),
    mapFile: z.string().nullable(),
    mapStatus: mapStatusSchema,
    mapLastModified: z.string().nullable(),
    mapLinkCount: z.number().int().min(0).nullable().optional().default(null)
  })
  .strict();

const siteSummaryListSchema = z.array(siteSummarySchema);

const mapFileEntrySchema = mapFileMetadataSchema.extend({
  filePath: z.string(),
  fileName: z.string(),
  isActive: z.boolean(),
  source: z.enum(['canonical', 'uploaded', 'legacy']),
  isCanonical: z.boolean()
});

const siteMapResponseSchema = z
  .object({
    site: z.string(),
    activeMap: z.string().nullable(),
    availableMaps: z.array(mapFileEntrySchema)
  })
  .strict();

const exportStatusSchema = z.object({
  jobId: z.string(),
  site: z.string(),
  script: z.string(),
  status: z.enum(['running', 'completed', 'unknown', 'succeeded', 'failed', 'queued', 'cancelled']),
  startedAt: z.string().nullable(),
  exitCode: z.number().nullable(),
  exitSignal: z.string().nullable(),
  lastEventAt: z.string().nullable(),
  progressPercent: z.number().nullable().optional(),
  processedUrls: z.number().nullable().optional(),
  totalUrls: z.number().nullable().optional(),
  successUrls: z.number().nullable().optional(),
  failedUrls: z.number().nullable().optional(),
  estimatedSecondsRemaining: z.number().nullable().optional()
});

const authUserBasicSchema = authUserSchema.pick({ id: true, username: true, role: true });

const queuedExportSchema = z.object({
  queueId: z.string(),
  site: z.string(),
  options: z.object({
    concurrency: z.number().optional(),
    resume: z.boolean().optional(),
    extraArgs: z.array(z.string()).optional(),
    limit: z.number().optional()
  }),
  requestedAt: z.string(),
  estimatedUrlCount: z.number().optional()
});

export type ProxyStats = z.infer<typeof proxyStatsSchema>;

interface ApiClientConfig {
  baseUrl?: string;
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      // ignore
    }
    const message = typeof (payload as { error?: string })?.error === 'string'
      ? (payload as { error: string }).error
      : `Запрос завершился с ошибкой ${response.status}`;
    throw new ApiError(message, response.status, payload);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

async function request<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  try {
    const response = await fetch(input, init);
    return await handleResponse<T>(response);
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    throw new NetworkError(error instanceof Error ? error.message : 'Неизвестная ошибка сети');
  }
}

async function requestWithCredentials(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, { credentials: 'include', ...init });
  } catch (error) {
    throw new NetworkError(error instanceof Error ? error.message : 'Неизвестная ошибка сети');
  }
}

async function handleErrorPayload(response: Response): Promise<{ message: string; body: unknown }>
{
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }

  const message = typeof (body as { error?: string })?.error === 'string'
    ? (body as { error: string }).error
    : response.status === 404
      ? 'Сводный отчёт ещё не создан'
      : `Не удалось получить сводный отчёт (${response.status})`;

  return { message, body };
}

export function createApiClient(config: ApiClientConfig = {}) {
  const base = config.baseUrl ?? '';

  const withBase = (path: string) => `${base}${path}`;

  return {
    startExport: async (site: string, exportConfig: ExportConfig) => {
      const payload = await request<unknown>(withBase(`/api/export/${site}`), {
        method: 'POST',
        headers: JSON_HEADERS,
        body: JSON.stringify(exportConfig)
      });
      return exportJobSchema.parse(payload);
    },
    stopExport: (jobId: string) =>
      request<void>(withBase(`/api/export/stop/${jobId}`), {
        method: 'POST',
        headers: JSON_HEADERS
      }),
    uploadJsonMap: async (site: string, file: File) => {
      const formData = new FormData();
      formData.append('site', site);
      formData.append('file', file);
      const result = await request<unknown>(withBase('/api/upload'), {
        method: 'POST',
        body: formData
      });
      return uploadResultSchema.parse(result);
    },
    downloadExport: async (site: string, options?: DownloadExportOptions) => {
      const params = new URLSearchParams();
      if (options?.format) {
        params.set('format', options.format);
      }
      if (options?.sheet) {
        params.set('sheet', options.sheet);
      }
      const query = params.toString();
      const response = await fetch(withBase(`/api/download/${site}${query ? `?${query}` : ''}`));
      if (!response.ok) {
        throw new ApiError('Не удалось получить файл экспорта', response.status);
      }
      return response.blob();
    },
    getProxyStats: async () => {
      const stats = await request<unknown>(withBase('/api/proxy/stats'));
      return proxyStatsSchema.parse(stats);
    },
    getSummaryMetrics: async () => {
      const payload = await request<unknown>(withBase('/api/summary'));
      return summaryResponseSchema.parse(payload);
    },
    getSites: async () => {
      const payload = await request<unknown>(withBase('/api/sites'));
      return siteSummaryListSchema.parse(payload);
    },
    getSiteDetail: async (site: string) => {
      const payload = await request<unknown>(withBase(`/api/sites/${site}`));
      return siteSummarySchema.parse(payload);
    },
    getSiteMaps: async (site: string) => {
      const payload = await request<unknown>(withBase(`/api/sites/${site}/maps`));
      return siteMapResponseSchema.parse(payload);
    },
    getExportStatus: async (jobId: string) => {
      const payload = await request<unknown>(withBase(`/api/export/status/${jobId}`));
      return exportStatusSchema.parse(payload);
    },
    downloadMasterWorkbook: async (onProgress?: (progress: DownloadProgress) => void) => {
      const response = await fetch(withBase('/api/download/master'));
      if (!response.ok) {
        const payload = await handleErrorPayload(response);
        throw new ApiError(payload.message, response.status, payload.body);
      }

      const totalHeader = response.headers.get('content-length');
      const total = totalHeader ? Number(totalHeader) : undefined;

      const emitProgress = (loadedValue: number, totalValue?: number) => {
        if (!onProgress) {
          return;
        }
        if (typeof totalValue === 'number') {
          onProgress({ loaded: loadedValue, total: totalValue });
        } else {
          onProgress({ loaded: loadedValue });
        }
      };

      if (!response.body) {
        const blob = await response.blob();
        emitProgress(blob.size, total);
        return blob;
      }

      const reader = response.body.getReader();
      const chunks: BlobPart[] = [];
      let loaded = 0;
      emitProgress(loaded, total);

      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        if (value) {
          chunks.push(value);
          loaded += value.length;
          emitProgress(loaded, total);
        }
      }

      const blob = new Blob(chunks, {
        type: response.headers.get('content-type') ?? 'application/octet-stream'
      });
      emitProgress(blob.size, total ?? blob.size);
      return blob;
    },
    getMasterWorkbookStatus: async () => {
      const payload = await request<unknown>(withBase('/api/download/master/status'));
      return masterWorkbookStatusSchema.parse(payload);
    },
    login: async (credentials: LoginCredentials) => {
      const input = loginSchema.parse(credentials);
      const response = await requestWithCredentials(withBase('/api/auth/login'), {
        method: 'POST',
        headers: JSON_HEADERS,
        body: JSON.stringify(input)
      });

      if (!response.ok) {
        let message = 'Не удалось выполнить вход';
        try {
          const payload = (await response.json()) as { error?: string };
          if (payload?.error) {
            message = payload.error;
          }
        } catch {
          // ignore json parse errors
        }
        throw new ApiError(message, response.status);
      }

      const data = (await response.json()) as unknown;
      return authUserBasicSchema.parse(data);
    },
    logout: async () => {
      const response = await requestWithCredentials(withBase('/api/auth/logout'), {
        method: 'POST'
      });
      if (!response.ok) {
        throw new ApiError('Не удалось выполнить выход', response.status);
      }
    },
    currentUser: async () => {
      const response = await requestWithCredentials(withBase('/api/auth/me'));
      if (response.status === 401) {
        return null;
      }
      if (!response.ok) {
        throw new ApiError('Не удалось получить данные пользователя', response.status);
      }
      const payload = (await response.json()) as unknown;
      return authUserSchema.parse(payload);
    },
    getExportQueue: async () => {
      const payload = await request<unknown>(withBase('/api/export/queue'));
      return z.array(queuedExportSchema).parse(payload);
    },
    cancelQueuedExport: async (queueId: string) => {
      await request<void>(withBase(`/api/export/queue/${queueId}`), {
        method: 'DELETE'
      });
    },
    getActiveExportJob: async (site: string) => {
      const payload = await request<unknown>(withBase(`/api/export/active/${site}`));
      return exportStatusSchema.parse(payload);
    }
  };
}

const defaultClient = createApiClient();

export const startExport = defaultClient.startExport;
export const stopExport = defaultClient.stopExport;
export const uploadJsonMap = defaultClient.uploadJsonMap;
export const downloadExport = defaultClient.downloadExport;
export const getProxyStats = defaultClient.getProxyStats;
export const getSummaryMetrics = defaultClient.getSummaryMetrics;
export const getSites = defaultClient.getSites;
export const getSiteDetail = defaultClient.getSiteDetail;
export const getExportStatus = defaultClient.getExportStatus;
export const getSiteMaps = defaultClient.getSiteMaps;
export const downloadMasterWorkbook = defaultClient.downloadMasterWorkbook;
export const getMasterWorkbookStatus = defaultClient.getMasterWorkbookStatus;
export const loginRequest = defaultClient.login;
export const logoutRequest = defaultClient.logout;
export const getCurrentUser = defaultClient.currentUser;
export const getExportQueue = defaultClient.getExportQueue;
export const cancelQueuedExport = defaultClient.cancelQueuedExport;
export const getActiveExportJob = defaultClient.getActiveExportJob;
