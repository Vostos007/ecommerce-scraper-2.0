'use client';

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { ExportForm } from './ExportForm';
import { LogViewer } from './LogViewer';
import type { AvailableMapEntry } from './MapStatus';
import { useExportJob } from '@/hooks/useExportJob';
import { downloadExport, getSiteDetail, getSiteMaps, type ExportConfig } from '@/lib/api';
import type { SiteSummary } from '@/lib/sites';
import { useDashboardStore } from '@/stores/dashboard';
import { Select } from './ui/select';
import { Button } from './ui/button';

interface SiteDashboardClientProps {
  site: string;
}

export function SiteDashboardClient({ site }: SiteDashboardClientProps) {
  const { setActiveSite } = useDashboardStore();
  const { startExport, status, latestJobId, isRunning } = useExportJob(site);
  const [downloadFormat, setDownloadFormat] = useState<'csv' | 'xlsx'>('csv');
  const [downloadSheet, setDownloadSheet] = useState<'full_data' | 'seo' | 'changes'>('full_data');
  const { data: siteDetails, error } = useQuery<SiteSummary, Error>({
    queryKey: ['site', site],
    queryFn: () => getSiteDetail(site),
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false
  });
  const mapsQuery = useQuery({
    queryKey: ['site', site, 'maps'],
    queryFn: () => getSiteMaps(site),
    staleTime: 60_000,
    refetchOnWindowFocus: false
  });

  const mapsLoading = mapsQuery.isLoading || mapsQuery.isFetching;

  const mappedAvailableMaps: AvailableMapEntry[] = mapsQuery.data
    ? mapsQuery.data.availableMaps.map((entry) => ({
        fileName: entry.fileName,
        filePath: entry.filePath,
        modified: entry.modified,
        size: entry.size,
        linkCount: entry.linkCount,
        source: entry.source,
        isActive: entry.isActive,
        isCanonical: entry.isCanonical
      }))
    : [];

  useEffect(() => {
    const currentSite = useDashboardStore.getState().activeSite;
    if (currentSite !== site) {
      setActiveSite(site);
    }
  }, [setActiveSite, site]);

  return (
    <div className="grid gap-8 lg:grid-cols-[2fr,1fr]">
      <div className="space-y-8">
        <section id="export" className="space-y-4 scroll-mt-32">
          <ExportForm
            site={site}
            script={siteDetails?.script ?? status.script ?? null}
            isRunning={isRunning}
            latestJobId={latestJobId}
            statusMessage={
              status.script || siteDetails?.script
                ? `script: scripts.${status.script ?? siteDetails?.script}`
                : null
            }
            errorMessage={status.error}
            mapStatus={siteDetails?.mapStatus ?? 'missing'}
            mapFile={siteDetails?.mapFile ?? null}
            mapLastModified={siteDetails?.mapLastModified ?? null}
            mapLinkCount={siteDetails?.mapLinkCount ?? null}
            mapsLoading={mapsLoading}
            availableMaps={mappedAvailableMaps}
            onRefreshMaps={() => {
              void mapsQuery.refetch();
            }}
            onStart={async ({ concurrency, resume, limit }) => {
              const options: ExportConfig = { concurrency, resume };
              if (typeof limit === 'number') {
                options.limit = limit;
              }
              await startExport(options);
            }}
          />
        </section>
        <section id="logs" className="space-y-3 scroll-mt-32">
          <div className="flex items-center justify-between">
            <h2 className="text-xl font-semibold text-foreground">Логи</h2>
            {latestJobId && <span className="text-xs text-muted-foreground">jobId: {latestJobId}</span>}
          </div>
          <LogViewer jobId={latestJobId} />
        </section>
      </div>
      <aside id="summary" className="space-y-4 scroll-mt-32">
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-semibold text-foreground">Сводка</h2>
          {siteDetails?.status && (
            <span className="rounded-full bg-secondary/40 px-2 py-0.5 text-xs capitalize text-foreground">
              {siteDetails.status.replace('_', ' ')}
            </span>
          )}
        </div>
        {error && (
          <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
            Не удалось загрузить информацию о сайте: {error.message}
          </div>
        )}
        <div className="space-y-3 rounded-lg border border-border bg-card/40 p-4 text-sm">
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">Скрипт</span>
            <code className="rounded bg-secondary/40 px-2 py-1 text-xs">python -u -m scripts.{siteDetails?.script ?? '—'}</code>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">Последний экспорт</span>
            <span>{siteDetails?.lastExport ? new Date(siteDetails.lastExport).toLocaleString('ru-RU') : 'нет данных'}</span>
          </div>
          <div className="space-y-2">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <Select
                aria-label="Формат выгрузки"
                className="w-full sm:w-40"
                value={downloadFormat}
                onChange={(event) => setDownloadFormat(event.target.value as 'csv' | 'xlsx')}
                options={[
                  { value: 'csv', label: 'CSV (рекомендуемая)' },
                  { value: 'xlsx', label: 'Excel' }
                ]}
              />
              {downloadFormat === 'csv' && (
                <Select
                  aria-label="Набор данных"
                  className="w-full sm:w-48"
                  value={downloadSheet}
                  onChange={(event) => setDownloadSheet(event.target.value as 'full_data' | 'seo' | 'changes')}
                  options={[
                    { value: 'full_data', label: 'Товары (full_data)' },
                    { value: 'seo', label: 'SEO (seo)' },
                    { value: 'changes', label: 'Изменения (changes)' }
                  ]}
                />
              )}
            </div>
            <Button
              type="button"
              variant="ghost"
              className="w-full justify-center border border-primary/40 bg-primary/10 text-xs text-primary hover:bg-primary/20"
              onClick={async () => {
                try {
                  const blob = await downloadExport(
                    site,
                    downloadFormat === 'csv'
                      ? { format: 'csv', sheet: downloadSheet }
                      : undefined
                  );
                  const url = URL.createObjectURL(blob);
                  try {
                    const anchor = document.createElement('a');
                    anchor.href = url;
                    const safeName = site.replace(/\./g, '-');
                    const suffix =
                      downloadFormat === 'csv'
                        ? `${downloadSheet}.csv`
                        : 'latest.xlsx';
                    anchor.download = `${safeName}-${suffix}`;
                    anchor.style.display = 'none';
                    document.body.append(anchor);
                    anchor.click();
                    anchor.remove();
                  } finally {
                    setTimeout(() => URL.revokeObjectURL(url), 1_000);
                  }
                } catch (downloadError) {
                  console.error('Не удалось скачать экспорт', downloadError);
                }
              }}
            >
              Скачать последний экспорт
            </Button>
          </div>
          {siteDetails?.mapFile && (
            <div className="space-y-1 text-xs text-muted-foreground">
              <div className="font-medium text-foreground">Карта сайта</div>
              <div>{siteDetails.mapFile}</div>
              {siteDetails.mapLastModified && <div>Обновлена: {new Date(siteDetails.mapLastModified).toLocaleString('ru-RU')}</div>}
              {typeof siteDetails.mapLinkCount === 'number' && <div>Ссылок: {siteDetails.mapLinkCount}</div>}
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}
