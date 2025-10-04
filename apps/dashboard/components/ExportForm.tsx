'use client';

import { useEffect, useMemo, useState } from 'react';

import { type UploadStatus } from '@/hooks/useFileUpload';

import { Button } from './ui/button';
import { Card, CardContent } from './ui/card';
import { Input } from './ui/input';
import { FileUpload } from './FileUpload';
import { MapStatus, type AvailableMapEntry } from './MapStatus';

type StartOptions = { concurrency: number; resume: boolean; limit?: number };

interface ExportFormProps {
  site: string;
  script?: string | null;
  isRunning: boolean;
  latestJobId: string | null;
  statusMessage: string | null;
  errorMessage: string | null;
  onStart: (options: StartOptions) => Promise<void>;
  mapStatus?: 'available' | 'missing' | 'outdated';
  mapFile?: string | null;
  mapLastModified?: string | null;
  mapLinkCount?: number | null;
  mapsLoading?: boolean;
  availableMaps?: AvailableMapEntry[];
  onRefreshMaps?: () => void;
}

const SITE_PRESETS: Record<string, { concurrency: number; estimatedDuration: string; notes?: string }> = {
  'atmospherestore.ru': { concurrency: 64, estimatedDuration: '≈45 минут' },
  'sittingknitting.ru': { concurrency: 48, estimatedDuration: '≈35 минут' },
  'mpyarn.ru': { concurrency: 48, estimatedDuration: '≈40 минут' },
  'ili-ili.com': { concurrency: 48, estimatedDuration: '≈50 минут' },
  'knitshop.ru': { concurrency: 2, estimatedDuration: '≈20 минут', notes: 'Сайт чувствителен к нагрузке' },
  'triskeli.ru': { concurrency: 4, estimatedDuration: '≈30 минут' },
  '6wool.ru': { concurrency: 3, estimatedDuration: '≈25 минут', notes: 'Возможны антибот проверки' }
};

export function ExportForm({
  site,
  script,
  isRunning,
  latestJobId,
  statusMessage,
  errorMessage,
  onStart,
  mapStatus = 'missing',
  mapFile = null,
  mapLastModified = null,
  mapLinkCount = null,
  mapsLoading,
  availableMaps,
  onRefreshMaps
}: ExportFormProps) {
  const preset = SITE_PRESETS[site] ?? { concurrency: 8, estimatedDuration: '≈45 минут' };
  const [concurrency, setConcurrency] = useState<number>(preset.concurrency);
  const [resume, setResume] = useState(false);
  const [limit, setLimit] = useState<string>('');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [uploadStatus, setUploadStatus] = useState<UploadStatus | null>(null);

  const scriptName = script ?? `${site.replace(/\./g, '_')}_fast_export`;

  useEffect(() => {
    setConcurrency(preset.concurrency);
    setLimit('');
    setFormError(null);
    setResume(false);
  }, [preset.concurrency, site]);

  const hasBlockingMapIssue = mapStatus === 'missing';
  const mapsLoadingFlag = Boolean(mapsLoading);

  useEffect(() => {
    if (uploadStatus?.success) {
      onRefreshMaps?.();
    }
  }, [onRefreshMaps, uploadStatus?.success]);

  const isStartDisabled = useMemo(() => {
    if (isRunning || uploadStatus?.isUploading) {
      return true;
    }
    if (uploadStatus?.error) {
      return true;
    }
    if (hasBlockingMapIssue) {
      return true;
    }
    return false;
  }, [hasBlockingMapIssue, isRunning, uploadStatus?.error, uploadStatus?.isUploading]);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);

    if (hasBlockingMapIssue) {
      setFormError('Карта сайта отсутствует. Загрузите JSON перед запуском экспорта.');
      return;
    }

    if (!Number.isFinite(concurrency) || concurrency < 1 || concurrency > 128) {
      setFormError('Concurrency должен быть в диапазоне 1-128');
      return;
    }

    let parsedLimit: number | undefined;
    if (limit.trim()) {
      const numeric = Number(limit.trim());
      if (!Number.isFinite(numeric) || numeric < 1) {
        setFormError('Limit должен быть положительным числом');
        return;
      }
      parsedLimit = Math.trunc(numeric);
    }

    const options: StartOptions = { concurrency, resume };
    if (parsedLimit !== undefined) {
      options.limit = parsedLimit;
    }

    await onStart(options);
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="space-y-6 rounded-xl border border-border bg-card/60 p-6"
    >
      <MapStatus
        site={site}
        mapStatus={mapStatus}
        mapFile={mapFile ?? null}
        mapLastModified={mapLastModified ?? null}
        mapLinkCount={mapLinkCount ?? null}
        isLoading={mapsLoadingFlag}
        availableMaps={availableMaps ?? []}
        {...(onRefreshMaps ? { onRefresh: onRefreshMaps } : {})}
      />

      <div className="space-y-1">
        <h2 className="text-lg font-semibold text-foreground">Запуск экспорта · {site}</h2>
        <p className="text-sm text-muted-foreground">
          Управляет subprocess <code>python -u -m scripts.{scriptName}</code> с рекомендованными
          параметрами.
        </p>
        <p className="text-xs text-muted-foreground">
          Рекомендованная параллельность: <strong>{preset.concurrency}</strong>. Ожидаемая длительность —{' '}
          {preset.estimatedDuration}
          {preset.notes ? ` · ${preset.notes}` : ''}
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-3">
          <div className="flex flex-col gap-2 text-sm">
            <label className="text-muted-foreground" htmlFor="concurrency">
              Concurrency
            </label>
            <Input
              id="concurrency"
              type="number"
              min={1}
              max={128}
              value={concurrency}
              onChange={(event) => setConcurrency(Number(event.target.value))}
            />
          </div>

          <label className="flex items-center gap-3 text-sm">
            <input
              type="checkbox"
              checked={resume}
              onChange={(event) => setResume(event.target.checked)}
              className="h-4 w-4 rounded border-border bg-background"
            />
            <span>Resume (продолжать с частичных файлов)</span>
          </label>

          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground"
            onClick={() => setAdvancedOpen((prev) => !prev)}
          >
            {advancedOpen ? 'Скрыть расширенные настройки' : 'Показать расширенные настройки'}
          </button>

          {advancedOpen && (
            <Card>
              <CardContent className="space-y-3 pt-4">
                <div className="flex flex-col gap-2 text-sm">
                  <label className="text-muted-foreground" htmlFor="limit">
                    Limit (опционально)
                  </label>
                  <Input
                    id="limit"
                    type="number"
                    min={1}
                    placeholder="Например 500"
                    value={limit}
                    onChange={(event) => setLimit(event.target.value)}
                  />
                </div>
                <p className="text-xs text-muted-foreground">
                  Limit ограничивает количество URL для обработки. Используйте для частичных экспортов или
                  повторных прогонов.
                </p>
              </CardContent>
            </Card>
          )}
        </div>

        <FileUpload key={site} site={site} onStatusChange={setUploadStatus} />
      </div>

      {formError && <div className="text-sm text-rose-400">{formError}</div>}
      {errorMessage && <div className="text-sm text-rose-400">Ошибка: {errorMessage}</div>}
      {statusMessage && <div className="text-sm text-muted-foreground">{statusMessage}</div>}

      <div className="flex items-center gap-4">
        <Button type="submit" disabled={isStartDisabled}>
          {isRunning ? 'Экспорт выполняется…' : 'Запустить экспорт'}
        </Button>
        <div className="text-xs text-muted-foreground font-mono">
          jobId: {latestJobId ?? '—'}
        </div>
      </div>
    </form>
  );
}
