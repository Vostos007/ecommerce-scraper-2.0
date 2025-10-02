'use client';

import { useMemo } from 'react';

import { formatBytes } from '@/lib/utils';

import { Badge } from './ui/badge';
import { Button } from './ui/button';
import { Card, CardContent, CardHeader, CardTitle } from './ui/card';

export interface AvailableMapEntry {
  fileName: string;
  filePath?: string;
  modified: string;
  size: number;
  linkCount: number | null;
  source: 'canonical' | 'uploaded' | 'legacy';
  isActive: boolean;
  isCanonical?: boolean;
}

export interface MapStatusProps {
  site: string;
  mapStatus: 'available' | 'missing' | 'outdated';
  mapFile: string | null;
  mapLastModified: string | null;
  mapLinkCount: number | null;
  isLoading?: boolean;
  onRefresh?: () => void;
  availableMaps?: AvailableMapEntry[];
}

const STATUS_META: Record<MapStatusProps['mapStatus'], { label: string; variant: 'success' | 'warning' | 'error' }>
  = {
    available: { label: 'Готово', variant: 'success' },
    outdated: { label: 'Устарело', variant: 'warning' },
    missing: { label: 'Отсутствует', variant: 'error' }
  };

function formatDate(value: string | null): string {
  if (!value) {
    return '—';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString('ru-RU');
}

export function MapStatus({
  site,
  mapStatus,
  mapFile,
  mapLastModified,
  mapLinkCount,
  isLoading,
  onRefresh,
  availableMaps
}: MapStatusProps) {
  const statusMeta = STATUS_META[mapStatus];

  const fallbackList = useMemo(() => {
    if (!availableMaps || availableMaps.length === 0) {
      return [] as AvailableMapEntry[];
    }
    return availableMaps.slice(0, 5);
  }, [availableMaps]);

  const refreshDisabled = isLoading || !onRefresh;

  return (
    <Card data-testid="map-status" className="bg-secondary/10">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="text-sm font-medium">JSON карта сайта ({site})</CardTitle>
        <div className="flex items-center gap-2">
          <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={refreshDisabled}
            onClick={() => onRefresh?.()}
          >
            {isLoading ? 'Обновляем…' : 'Обновить'}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="flex flex-col gap-1">
          <span className="text-muted-foreground">Активная карта</span>
          <span className="font-mono text-xs">
            {mapFile ?? 'Не выбрана'}
          </span>
          <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
            <span>Обновлена: {formatDate(mapLastModified)}</span>
            <span>
              Ссылок: {mapLinkCount != null ? mapLinkCount : '—'}
            </span>
          </div>
        </div>

        {mapStatus !== 'available' && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
            {mapStatus === 'missing'
              ? 'Карта не найдена. Загрузите актуальный JSON файл в разделе ниже, прежде чем запускать экспорт.'
              : 'Текущая карта устарела. Рекомендуем загрузить новую актуальную карту.'}
          </div>
        )}

        {fallbackList.length > 0 && (
          <div className="space-y-2">
            <div className="text-xs text-muted-foreground uppercase tracking-wide">Доступные карты</div>
            <ul className="space-y-2">
              {fallbackList.map((entry) => (
                <li
                  key={`${entry.fileName}-${entry.modified}`}
                  className="rounded-md border border-border/40 bg-background/80 px-3 py-2 text-xs"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[11px]">
                      {entry.fileName}{' '}
                      {entry.isActive && <span className="text-emerald-300">(active)</span>}
                    </span>
                    <Badge variant={entry.source === 'canonical' ? 'primary' : entry.source === 'uploaded' ? 'success' : 'outline'}>
                      {entry.source === 'canonical'
                        ? 'canonical'
                        : entry.source === 'uploaded'
                          ? 'uploaded'
                          : 'legacy'}
                    </Badge>
                  </div>
                  <div className="flex flex-wrap gap-3 text-muted-foreground">
                    <span>{formatDate(entry.modified)}</span>
                    <span>{formatBytes(entry.size)}</span>
                    <span>links: {entry.linkCount ?? '—'}</span>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
