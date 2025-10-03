'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  ApiError,
  downloadBulkArchive,
  getBulkRunLatest,
  getBulkRunStatus,
  startBulkExport,
  type BulkRunSnapshot
} from '@/lib/api';

function formatError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return 'Неизвестная ошибка';
}

export interface BulkRunHook {
  activeRunId: string | null;
  snapshot: BulkRunSnapshot | null;
  isLoading: boolean;
  isStarting: boolean;
  error: string | null;
  start: (sites?: string[]) => Promise<void>;
  downloadArchive: () => Promise<void>;
}

function useEventSource(runId: string | null, onMessage: (snapshot: BulkRunSnapshot) => void) {
  useEffect(() => {
    if (!runId) {
      return;
    }

    const source = new EventSource(`/api/export/bulk/${runId}/stream`);
    const handler = (event: MessageEvent<string>) => {
      try {
        const payload = JSON.parse(event.data) as unknown;
        const snapshot = payload as BulkRunSnapshot;
        onMessage(snapshot);
      } catch (error) {
        console.error('[bulk-run] failed to parse SSE payload', error);
      }
    };

    source.addEventListener('snapshot', handler);
    source.onerror = () => {
      source.close();
    };

    return () => {
      source.removeEventListener('snapshot', handler);
      source.close();
    };
  }, [runId, onMessage]);
}

export function useBulkRun(): BulkRunHook {
  const queryClient = useQueryClient();
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Fetch latest run on mount
  useQuery<BulkRunSnapshot | null, Error>({
    queryKey: ['bulk-run-latest'],
    queryFn: async () => {
      const latest = await getBulkRunLatest();
      if (latest) {
        setActiveRunId(latest.id);
        queryClient.setQueryData(['bulk-run-status', latest.id], latest);
      }
      return latest;
    },
    staleTime: 30_000,
    refetchOnWindowFocus: false
  });

  const statusQuery = useQuery<BulkRunSnapshot | null, Error>({
    queryKey: ['bulk-run-status', activeRunId],
    queryFn: async () => {
      if (!activeRunId) {
        return null;
      }
      const snapshot = await getBulkRunStatus(activeRunId);
      return snapshot;
    },
    enabled: Boolean(activeRunId),
    refetchInterval: (query) => {
      const snapshot = query.state.data;
      if (!snapshot) {
        return false;
      }
      return snapshot.status === 'running' ? 5_000 : false;
    },
    refetchOnWindowFocus: false,
    staleTime: 5_000
  });

  useEventSource(activeRunId, (snapshot) => {
    setActiveRunId(snapshot.id);
    queryClient.setQueryData(['bulk-run-status', snapshot.id], snapshot);
  });

  const startMutation = useMutation({
    mutationFn: async (sites?: string[]) => {
      setError(null);
      try {
        const { runId, snapshot } = await startBulkExport(sites && sites.length > 0 ? { sites } : undefined);
        setActiveRunId(runId);
        queryClient.setQueryData(['bulk-run-status', runId], snapshot);
      } catch (err) {
        setError(formatError(err));
        if (err instanceof ApiError && err.status === 409 && err.payload && typeof err.payload === 'object') {
          const active = (err.payload as { activeRun?: BulkRunSnapshot }).activeRun;
          if (active) {
            setActiveRunId(active.id);
            queryClient.setQueryData(['bulk-run-status', active.id], active);
          }
        }
        throw err;
      }
    }
  });

  const snapshot = statusQuery.data ?? null;

  const downloadArchive = useCallback(async () => {
    if (!snapshot || snapshot.status !== 'completed' || !snapshot.id) {
      throw new Error('Архив доступен только после завершения массового прогона');
    }
    const blob = await downloadBulkArchive(snapshot.id);
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const filename = `bulk-run-${snapshot.id}-${timestamp}.zip`;
    const url = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      anchor.style.display = 'none';
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    } finally {
      setTimeout(() => URL.revokeObjectURL(url), 1_000);
    }
  }, [snapshot]);

  const start = useCallback(
    async (sites?: string[]) => {
      await startMutation.mutateAsync(sites);
    },
    [startMutation]
  );

  const hookError = useMemo(() => error ?? statusQuery.error?.message ?? null, [error, statusQuery.error]);

  return {
    activeRunId,
    snapshot,
    isLoading: statusQuery.isLoading,
    isStarting: startMutation.isPending,
    error: hookError,
    start,
    downloadArchive
  };
}
