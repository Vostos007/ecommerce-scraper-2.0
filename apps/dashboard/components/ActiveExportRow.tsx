'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { getExportStatus, stopExport, type ExportStatus } from '../lib/api';
import { estimateExportDuration, formatDurationExact } from '@/lib/export-estimates';
import { formatDateTime } from '@/lib/utils';
import { Button } from './ui/button';
import { Progress } from './ui/progress';

interface ActiveExportRowProps {
  jobId: string;
  siteName: string;
  siteDomain: string;
  mapLinkCount: number | null;
}

export function ActiveExportRow({ jobId, siteName, siteDomain, mapLinkCount }: ActiveExportRowProps) {
  const queryClient = useQueryClient();
  const statusQuery = useQuery<ExportStatus, Error>({
    queryKey: ['export-status', jobId],
    queryFn: async () => {
      return await getExportStatus(jobId);
    },
    refetchInterval: (query) =>
      query.state.data && ['running', 'queued'].includes(query.state.data.status) ? 5000 : false,
    retry: false
  });

  const stopMutation = useMutation({
    mutationFn: async () => {
      return await stopExport(jobId);
    },
    onSuccess: () => {
      // Invalidate the status query to refresh the data
      statusQuery.refetch().catch(() => undefined);
      queryClient.invalidateQueries({ queryKey: ['export-active', siteDomain] }).catch(() => undefined);
    },
    onError: (error: unknown) => {
      console.error('Failed to stop export:', error);
    }
  });

  const exportStatus = statusQuery.data;
  const isLoading = statusQuery.isLoading;
  const error = statusQuery.error;

  if (isLoading) {
    return (
      <div className="flex items-center justify-between p-4 border border-border rounded-lg bg-card/60">
        <div className="flex flex-col gap-2">
          <div className="text-sm font-medium">{siteName}</div>
          <div className="text-xs text-muted-foreground">jobId: {jobId}</div>
        </div>
        <div className="text-sm text-muted-foreground">Загрузка...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-between p-4 border border-border rounded-lg bg-card/60">
        <div className="flex flex-col gap-2">
          <div className="text-sm font-medium">{siteName}</div>
          <div className="text-xs text-muted-foreground">jobId: {jobId}</div>
        </div>
        <div className="text-sm text-rose-400">Ошибка загрузки статуса</div>
      </div>
    );
  }

  if (!exportStatus) {
    return null;
  }

  const progressPercent = exportStatus.progressPercent ?? 0;
  const processedUrls = exportStatus.processedUrls ?? 0;
  const totalUrls = exportStatus.totalUrls ?? null;
  const successUrls = exportStatus.successUrls ?? 0;
  const failedUrls = exportStatus.failedUrls ?? 0;
  const startedAtLabel = formatDateTime(exportStatus.startedAt, '—');
  const lastEventLabel = formatDateTime(exportStatus.lastEventAt, '—');

  const estimate = estimateExportDuration(siteDomain, mapLinkCount);
  const estimatedSecondsRemaining = exportStatus.estimatedSecondsRemaining ?? null;
  const totalEstimatedSeconds = estimate.durationSeconds;
  const totalUrlsEstimate = exportStatus.totalUrls ?? estimate.urlCount ?? null;
  const startedAtTime = exportStatus.startedAt ? Date.parse(exportStatus.startedAt) : NaN;
  const elapsedSeconds = Number.isFinite(startedAtTime) ? Math.max(0, (Date.now() - startedAtTime) / 1000) : null;

  let etaSeconds = estimatedSecondsRemaining;

  if (etaSeconds === null && totalUrlsEstimate && processedUrls > 0 && elapsedSeconds && elapsedSeconds >= 10) {
    const speed = processedUrls / elapsedSeconds;
    if (speed > 0) {
      const remainingUrls = Math.max(totalUrlsEstimate - processedUrls, 0);
      const projection = remainingUrls / speed;
      if (Number.isFinite(projection)) {
        etaSeconds = Math.round(projection);
      }
    }
  }

  if (etaSeconds === null && totalEstimatedSeconds) {
    const progressRatio = (() => {
      if (totalUrlsEstimate && totalUrlsEstimate > 0) {
        return Math.min(1, processedUrls / totalUrlsEstimate);
      }
      if (exportStatus.progressPercent != null) {
        return Math.min(1, exportStatus.progressPercent / 100);
      }
      return null;
    })();
    if (progressRatio !== null) {
      etaSeconds = Math.max(Math.round(totalEstimatedSeconds * (1 - progressRatio)), 0);
    }
  }

  if (etaSeconds === null && totalEstimatedSeconds && elapsedSeconds !== null) {
    etaSeconds = Math.max(Math.round(totalEstimatedSeconds - elapsedSeconds), 0);
  }

  const etaLabel = (() => {
    if (!exportStatus) {
      return null;
    }
    if (exportStatus.status === 'succeeded') {
      return 'Готово';
    }
    if (exportStatus.status === 'failed') {
      return 'Завершено с ошибкой';
    }
    if (etaSeconds !== null) {
      return `Осталось ≈ ${formatDurationExact(etaSeconds)}`;
    }
    return exportStatus.status === 'running' ? 'Расчёт ETA…' : null;
  })();

  // Progress bar color is handled by CSS based on status

  const statusLabels: Record<string, string> = {
    queued: 'в очереди',
    running: 'выполняется',
    succeeded: 'успешно',
    failed: 'ошибка',
    cancelled: 'отменено',
    unknown: 'неизвестно'
  };
  const statusLabel = statusLabels[exportStatus.status] ?? exportStatus.status;

  const isRunning = exportStatus.status === 'running';
  const canStop = isRunning;

  return (
    <div className="flex items-center gap-4 p-4 border border-border rounded-lg bg-card/60">
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between mb-2">
          <div className="text-sm font-medium truncate">{siteName}</div>
          <div className="text-xs uppercase tracking-wide text-muted-foreground">
            {statusLabel}
          </div>
        </div>

        <div className="mb-2">
          <Progress value={progressPercent} />
        </div>

        <div className="flex flex-col gap-1 text-xs text-muted-foreground">
          <div className="flex items-center justify-between gap-3">
            <span>
              Обработано {processedUrls}
              {typeof totalUrls === 'number' ? ` из ${totalUrls}` : ''} URL
              {successUrls > 0 && ` · успехов ${successUrls}`}
              {failedUrls > 0 && ` · ошибок ${failedUrls}`}
            </span>
            <span className="font-mono">{jobId}</span>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-[11px]">
            <span>Старт: {startedAtLabel}</span>
            <span className="text-muted-foreground/70">Последнее событие: {lastEventLabel}</span>
            <span className="text-muted-foreground/70">
              Оценка: {estimate.durationLabel}
              {estimate.urlCountLabel ? ` · ${estimate.urlCountLabel} URL` : ''}
            </span>
            {etaLabel && <span className="text-muted-foreground/70">{etaLabel}</span>}
          </div>
        </div>
      </div>

      <Button
        variant="destructive"
        size="sm"
        onClick={() => stopMutation.mutate()}
        disabled={!canStop || stopMutation.isPending}
      >
        {stopMutation.isPending ? 'Остановка...' : 'Остановить'}
      </Button>
    </div>
  );
}
