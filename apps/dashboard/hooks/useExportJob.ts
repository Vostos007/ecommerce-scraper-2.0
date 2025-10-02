'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, getExportStatus, startExport } from '../lib/api';
import type { ExportConfig, ExportJob, ExportStatus } from '../lib/api';

interface ExportState {
  job: ExportJob | null;
  error: string | null;
}

const initialState: ExportState = {
  job: null,
  error: null
};

export function useExportJob(site: string | null = null) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<ExportState>(initialState);
  const refreshMarkerRef = useRef<string | null>(null);

  const mutation = useMutation({
    mutationFn: async (config: ExportConfig) => {
      if (!site) {
        throw new Error('Сайт не выбран');
      }
      return await startExport(site, config);
    },
    onSuccess: (job) => {
      setState({ job, error: null });
      queryClient.invalidateQueries({ queryKey: ['exports', 'list'] }).catch(() => undefined);
    },
    onError: (error: unknown) => {
      setState({ job: null, error: error instanceof Error ? error.message : 'Неизвестная ошибка' });
    }
  });

  const start = useCallback(
    async (config: ExportConfig = {}) => {
      setState(initialState);
      await mutation.mutateAsync(config);
    },
    [mutation]
  );

  const jobId = state.job?.jobId ?? null;

  const statusQuery = useQuery<ExportStatus, Error>({
    queryKey: ['export-status', jobId],
    enabled: Boolean(jobId),
    queryFn: async () => {
      if (!jobId) {
        throw new Error('jobId is required');
      }
      try {
        return await getExportStatus(jobId);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          const fallbackSite = state.job?.site ?? site ?? 'unknown-site';
          const fallbackScript =
            state.job?.script ?? `${fallbackSite.replace(/\./g, '_')}_fast_export`;
          return {
            jobId,
            site: fallbackSite,
            script: fallbackScript,
            status: 'unknown',
            startedAt: state.job?.startedAt ?? null,
            exitCode: null,
            exitSignal: null,
            lastEventAt: null
          } satisfies ExportStatus;
        }
        throw error;
      }
    },
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 5000 : false),
    retry: false
  });

  useEffect(() => {
    const statusData = statusQuery.data;
    const activeJobId = statusData?.jobId ?? state.job?.jobId ?? null;

    if (!activeJobId) {
      refreshMarkerRef.current = null;
      return;
    }

    if (statusData?.status === 'running') {
      refreshMarkerRef.current = null;
      return;
    }

    if (!statusData) {
      return;
    }

    const marker = [
      activeJobId,
      statusData.status,
      statusData.exitCode ?? 'null',
      statusData.lastEventAt ?? 'null'
    ].join('|');

    if (refreshMarkerRef.current === marker) {
      return;
    }

    refreshMarkerRef.current = marker;

    void queryClient.invalidateQueries({ queryKey: ['summary-metrics'] });
    void queryClient.invalidateQueries({ queryKey: ['sites'] });
    void queryClient.invalidateQueries({ queryKey: ['sites-for-download'] });

    if (site) {
      void queryClient.invalidateQueries({ queryKey: ['site', site] });
      void queryClient.invalidateQueries({ queryKey: ['site', site, 'maps'] });
    }
  }, [queryClient, site, state.job?.jobId, statusQuery.data]);

  return useMemo(
    () => ({
      startExport: start,
      status: {
        jobId: state.job?.jobId ?? null,
        site: state.job?.site ?? site,
        script: state.job?.script ?? null,
        startedAt: state.job?.startedAt ?? null,
        error: state.error,
        exportStatus: statusQuery.data ?? null,
        statusError: statusQuery.error?.message ?? null
      },
      latestJobId: jobId,
      isRunning: Boolean(statusQuery.data?.status === 'running' || mutation.isPending),
      isPolling: statusQuery.isFetching,
      exportStatus: statusQuery.data ?? null
    }),
    [
      jobId,
      mutation.isPending,
      site,
      start,
      state.error,
      state.job,
      statusQuery.data,
      statusQuery.error,
      statusQuery.isFetching
    ]
  );
}
