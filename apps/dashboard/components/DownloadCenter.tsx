'use client';

import { useCallback, useMemo, useState } from 'react';

import { useMutation, useQuery } from '@tanstack/react-query';

import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Select } from '@/components/ui/select';
import { useMasterWorkbook } from '@/hooks/useMasterWorkbook';
import { downloadExport, getSites, type DownloadExportOptions } from '@/lib/api';
import type { SiteSummary } from '@/lib/sites';

const dateFormatter = new Intl.DateTimeFormat('ru-RU', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit'
});

const CSV_SHEET_OPTIONS = [
  { value: 'full_data', label: 'full_data.csv — товары' },
  { value: 'seo', label: 'seo.csv — SEO-поля' },
  { value: 'changes', label: 'changes.csv — сравнение с прошлым' }
] as const;

const DEFAULT_CSV_SHEET = CSV_SHEET_OPTIONS[0].value;

type CsvSheet = (typeof CSV_SHEET_OPTIONS)[number]['value'];

export function DownloadCenter() {
  const sitesQuery = useQuery<SiteSummary[], Error>({
    queryKey: ['sites-for-download'],
    queryFn: getSites,
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false
  });

  const masterWorkbook = useMasterWorkbook();
  const progress = masterWorkbook.progress;
  const progressPercent = progress?.total
    ? Math.round((progress.loaded / progress.total) * 100)
    : null;
  const showProgress = masterWorkbook.isGenerating || masterWorkbook.isDownloading || Boolean(progress);

  const [csvSelections, setCsvSelections] = useState<Record<string, CsvSheet>>({});

  const siteDownload = useMutation<
    void,
    Error,
    { site: SiteSummary; format: 'xlsx' | 'csv'; sheet?: CsvSheet }
  >({
    mutationFn: async ({ site, format, sheet }) => {
      const sheetValue: CsvSheet = sheet ?? DEFAULT_CSV_SHEET;
      const options: DownloadExportOptions | undefined =
        format === 'csv' ? { format: 'csv', sheet: sheetValue } : undefined;
      const blob = await downloadExport(site.domain, options);
      const url = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement('a');
        anchor.href = url;
        const safeName = site.domain.replace(/\./g, '-');
        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        const suffix = format === 'csv' ? sheetValue : 'latest';
        const extension = format === 'csv' ? 'csv' : 'xlsx';
        anchor.download = `${safeName}-${suffix}-${timestamp}.${extension}`;
        anchor.style.display = 'none';
        document.body.append(anchor);
        anchor.click();
        anchor.remove();
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1_000);
      }
    }
  });

  const handleDownload = useCallback(
    (site: SiteSummary, format: 'xlsx' | 'csv', sheet?: CsvSheet) => {
      if (siteDownload.isPending) {
        return;
      }
      siteDownload.reset();
      const payload =
        sheet !== undefined
          ? { site, format, sheet }
          : { site, format };
      siteDownload.mutate(payload);
    },
    [siteDownload]
  );

  const handleCsvSelection = useCallback((domain: string, value: string) => {
    setCsvSelections((previous) => ({ ...previous, [domain]: value as CsvSheet }));
  }, []);

  const getCsvSheet = useCallback(
    (domain: string) => csvSelections[domain] ?? DEFAULT_CSV_SHEET,
    [csvSelections]
  );

  const statusMessage = useMemo(() => {
    if (masterWorkbook.error) {
      return masterWorkbook.error;
    }
    if (masterWorkbook.isGenerating) {
      return 'Генерируем сводный отчёт…';
    }
    if (masterWorkbook.fileInfo?.generatedAt) {
      try {
        return `Обновлено: ${dateFormatter.format(new Date(masterWorkbook.fileInfo.generatedAt))}`;
      } catch {
        return `Обновлено: ${masterWorkbook.fileInfo.generatedAt}`;
      }
    }
    return 'Сводный отчёт ещё не создавался';
  }, [masterWorkbook.error, masterWorkbook.fileInfo, masterWorkbook.isGenerating]);

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Сводный master workbook</CardTitle>
          <CardDescription>
            Объединяет актуальные выгрузки всех площадок в один Excel файл
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4 pt-0">
          <div className="flex items-center justify-between gap-4">
            <div className="space-y-1 text-sm text-muted-foreground">
              <div className="flex items-center gap-2">
                <p>{statusMessage}</p>
                {masterWorkbook.isStale && (
                  <Badge variant="warning">Устарел</Badge>
                )}
              </div>
              {masterWorkbook.phase && (
                <p className="text-xs text-muted-foreground">Этап: {masterWorkbook.phase}</p>
              )}
              {masterWorkbook.fileInfo?.size ? (
                <p>Размер: {(masterWorkbook.fileInfo.size / (1024 * 1024)).toFixed(1)} MB</p>
              ) : null}
            </div>
            <Button
              type="button"
              onClick={masterWorkbook.triggerDownload}
              disabled={masterWorkbook.isDownloading || masterWorkbook.isGenerating}
            >
              {masterWorkbook.isDownloading ? 'Скачиваем…' : 'Скачать отчёт'}
            </Button>
          </div>
          {showProgress && (
            <div className="space-y-1">
              <Progress
                {...(progressPercent != null ? { value: progressPercent } : {})}
                indeterminate={progressPercent == null}
                aria-label="Подготовка сводного отчёта"
              />
              {progressPercent != null && (
                <p className="text-xs text-muted-foreground">Скачано: {progressPercent}%</p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Индивидуальные выгрузки</CardTitle>
          <CardDescription>
            Скачайте последний Excel для любой площадки или перезапустите экспорт через панель
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-0">
          {sitesQuery.isLoading && (
            <div className="space-y-2">
              {Array.from({ length: 4 }).map((_, index) => (
                <div key={index} className="h-12 animate-pulse rounded-md bg-slate-800/40" />
              ))}
            </div>
          )}

          {sitesQuery.error && (
            <p className="text-sm text-rose-300">
              Не удалось загрузить список сайтов: {sitesQuery.error.message}
            </p>
          )}

          {sitesQuery.data && (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {sitesQuery.data.map((site) => {
                const lastExport = site.lastExport ? dateFormatter.format(new Date(site.lastExport)) : 'нет данных';
                return (
                  <div
                    key={site.domain}
                    className="flex items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-900/40 p-3"
                  >
                    <div className="space-y-2">
                      <p className="font-medium">{site.name}</p>
                      <p className="text-xs text-muted-foreground">{site.domain}</p>
                      <div className="flex items-center gap-2 text-xs text-muted-foreground">
                        <span>Последний экспорт:</span>
                        <Badge variant={site.status === 'ready' ? 'success' : site.status === 'missing_export' ? 'warning' : 'error'}>
                          {lastExport}
                        </Badge>
                      </div>
                      <Select
                        size="sm"
                        value={getCsvSheet(site.domain)}
                        onChange={(event) => handleCsvSelection(site.domain, event.target.value)}
                        options={CSV_SHEET_OPTIONS.map(({ value, label }) => ({ value, label }))}
                      />
                    </div>
                    <div className="flex flex-col gap-2">
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        onClick={() => handleDownload(site, 'csv', getCsvSheet(site.domain))}
                        disabled={siteDownload.isPending}
                      >
                        {siteDownload.isPending ? 'Подготавливаем…' : 'Скачать CSV'}
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        onClick={() => handleDownload(site, 'xlsx')}
                        disabled={siteDownload.isPending}
                      >
                        {siteDownload.isPending ? 'Подготавливаем…' : 'Скачать Excel'}
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
