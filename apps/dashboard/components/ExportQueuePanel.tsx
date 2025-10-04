'use client';

import { useMemo } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { cancelQueuedExport, getExportQueue, type QueuedExport } from '@/lib/api';
import { getSiteExportPreset } from '@/lib/export-presets';
import { estimateExportDuration, formatUrlCount } from '@/lib/export-estimates';
import { formatDateTime } from '@/lib/utils';
import { useActiveExports } from '@/hooks/useActiveExports';
import { MAX_CONCURRENT_EXPORTS } from '@/lib/export-constants';
import { Button } from './ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './ui/card';

export function ExportQueuePanel() {
  const queryClient = useQueryClient();
  const { sites, activeJobs } = useActiveExports();
  const siteLookup = useMemo(() => new Map(sites.map((site) => [site.domain, site])), [sites]);

  const queueQuery = useQuery<QueuedExport[], Error>({
    queryKey: ['export-queue'],
    queryFn: async () => {
      return await getExportQueue();
    },
    refetchInterval: 5000
  });

  const cancelMutation = useMutation({
    mutationFn: async (queueId: string) => {
      await cancelQueuedExport(queueId);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['export-queue'] }).catch(() => undefined);
    }
  });

  const isLoading = queueQuery.isLoading;
  const error = queueQuery.error;
  const queue = useMemo(() =>
    (queueQuery.data ?? []).slice().sort((a, b) => a.requestedAt.localeCompare(b.requestedAt)),
  [queueQuery.data]);
  const pipelineCaption = useMemo(() => {
    const running = activeJobs.length;
    const names = queue.map((entry) => entry.site).join(' → ');
    if (!names) {
      return `Сейчас запущено ${running}/${MAX_CONCURRENT_EXPORTS}`;
    }
    return `Сейчас запущено ${running}/${MAX_CONCURRENT_EXPORTS}. В очереди: ${names}`;
  }, [activeJobs.length, queue]);

  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Очередь экспортов</CardTitle>
          <CardDescription>Запросы стоят в ожидании свободного слота</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="text-sm text-muted-foreground">Загрузка…</div>
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Очередь экспортов</CardTitle>
          <CardDescription>Запросы стоят в ожидании свободного слота</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="text-sm text-rose-400">
            Ошибка загрузки: {error.message}
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!queue.length) {
    return null;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Очередь экспортов</CardTitle>
        <CardDescription>{pipelineCaption}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {queue.map((entry, index) => {
          const preset = getSiteExportPreset(entry.site);
          const siteSummary = siteLookup.get(entry.site);
          const estimate = estimateExportDuration(entry.site, siteSummary?.mapLinkCount ?? entry.estimatedUrlCount ?? null);
          const concurrencyLabel = entry.options.concurrency ?? preset.concurrency;
          const resumeLabel = entry.options.resume ?? false;
          const limitLabel = entry.options.limit;
          const estimatedDurationLabel = estimate.durationLabel ?? preset.estimatedDuration;
          const estimatedUrlsLabel = estimate.urlCountLabel ?? formatUrlCount(entry.estimatedUrlCount ?? null);
          return (
            <div
              key={entry.queueId}
              className="flex flex-col gap-2 rounded-lg border border-border/60 bg-card/40 p-3 text-sm"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">
                  {index + 1}. {entry.site}
                </span>
                <span className="text-xs text-muted-foreground font-mono">{entry.queueId}</span>
              </div>
              <div className="text-xs text-muted-foreground space-y-1">
                <p>
                  Ожидает с {formatDateTime(entry.requestedAt)} · оценка — {estimatedDurationLabel}
                  {estimatedUrlsLabel ? ` · ${estimatedUrlsLabel} URL` : ''}
                </p>
                <p>
                  Конкурентность: {concurrencyLabel} · Resume: {resumeLabel ? 'включено' : 'выключено'}
                  {typeof limitLabel === 'number' ? ` · Лимит: ${limitLabel}` : ''}
                </p>
              </div>
              <div className="flex items-center justify-end">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => cancelMutation.mutate(entry.queueId)}
                  disabled={cancelMutation.isPending}
                >
                  Отменить
                </Button>
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
