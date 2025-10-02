'use client';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './ui/card';
import { ActiveExportRow } from './ActiveExportRow';
import { useActiveExports } from '@/hooks/useActiveExports';

export function ActiveExportsPanel() {
  const { activeJobs, isLoading, error } = useActiveExports();

  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Активные экспорты</CardTitle>
          <CardDescription>Собираем данные о запущенных заданиях…</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="text-sm text-muted-foreground">Загрузка...</div>
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Активные экспорты</CardTitle>
          <CardDescription>Не удалось получить список активных запусков</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="text-sm text-rose-400">
            Ошибка загрузки: {error instanceof Error ? error.message : 'Неизвестная ошибка'}
          </div>
        </CardContent>
      </Card>
    );
  }

  // Hide panel when no active jobs
  if (activeJobs.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Активные экспорты</CardTitle>
        <CardDescription>Обновляем прогресс каждые 5 секунд</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-4">
          {activeJobs.map(({ job, site }) => (
            <ActiveExportRow
              key={job.jobId}
              jobId={job.jobId}
              siteName={site.name || site.domain}
              siteDomain={site.domain}
              mapLinkCount={site.mapLinkCount ?? null}
            />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
