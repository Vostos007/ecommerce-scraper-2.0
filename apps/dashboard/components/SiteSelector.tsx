'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';

import type { SiteSummary } from '@/lib/sites';
import { cn } from '@/lib/utils';
import { useDashboardStore } from '@/stores/dashboard';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './ui/card';
import { Input } from './ui/input';
import { Select } from './ui/select';
import { Badge } from './ui/badge';

type SortOption = 'name' | 'lastExport';

interface SiteSelectorProps {
  sites: SiteSummary[];
}

const sortOptions: Array<{ value: SortOption; label: string }> = [
  { value: 'name', label: 'По названию' },
  { value: 'lastExport', label: 'По дате экспорта' }
];

const statusColors: Record<SiteSummary['status'], string> = {
  ready: 'bg-emerald-500/70',
  missing_export: 'bg-amber-500/70',
  unknown: 'bg-slate-500/70',
  missing_map: 'bg-rose-500/70'
};

const mapStatusMeta: Record<'available' | 'missing' | 'outdated', { label: string; variant: 'success' | 'error' | 'warning' }>
  = {
    available: { label: 'карта готова', variant: 'success' },
    missing: { label: 'нет карты', variant: 'error' },
    outdated: { label: 'карта устарела', variant: 'warning' }
  };

export function SiteSelector({ sites }: SiteSelectorProps) {
  const { activeSite, setActiveSite } = useDashboardStore();
  const [query, setQuery] = useState('');
  const [sortBy, setSortBy] = useState<SortOption>('name');

  const filteredSites = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    const result = normalizedQuery
      ? sites.filter((site) =>
          [site.name, site.domain].some((value) => value.toLowerCase().includes(normalizedQuery))
        )
      : sites;

    return result
      .slice()
      .sort((a, b) => {
        if (sortBy === 'name') {
          return a.name.localeCompare(b.name);
        }
        const aTime = a.lastExport ? Date.parse(a.lastExport) : 0;
        const bTime = b.lastExport ? Date.parse(b.lastExport) : 0;
        return bTime - aTime;
      });
  }, [query, sites, sortBy]);

  return (
    <section className="space-y-4" data-testid="site-selector">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-xl font-semibold">Сайты</h2>
          <p className="text-sm text-muted-foreground">
            Выберите сайт для управления экспортами и мониторинга состояния.
          </p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <Input
            placeholder="Поиск по названию или домену"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="w-full sm:w-72"
            data-testid="site-search"
          />
          <Select
            aria-label="Сортировка"
            value={sortBy}
            onChange={(event) => setSortBy(event.target.value as SortOption)}
            options={sortOptions.map((option) => ({
              value: option.value,
              label: option.label
            }))}
            className="w-full sm:w-48"
            data-testid="site-sort"
          />
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {filteredSites.map((site) => {
          const isActive = site.domain === activeSite;
          const statusColor = statusColors[site.status] ?? statusColors.unknown;
          const lastExport = site.lastExport ? new Date(site.lastExport).toLocaleString('ru-RU') : 'нет данных';

          return (
            <Link
              key={site.domain}
              href={`/dashboard/${site.domain}`}
              data-testid={`site-card-${site.domain}`}
              onClick={() => setActiveSite(site.domain)}
              className="group"
            >
              <Card className={cn('h-full border border-border transition-all', isActive && 'border-primary shadow-lg')}
              >
                <CardHeader className="space-y-1">
                  <div className="flex items-center justify-between">
                    <CardTitle className="text-lg font-semibold text-foreground">{site.name}</CardTitle>
                    <span
                      className={cn('h-2 w-2 rounded-full transition-colors', statusColor)}
                      aria-label={site.status}
                    />
                  </div>
                  <CardDescription className="text-xs text-muted-foreground">
                    {site.domain}
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3 text-sm">
                  <div className="flex items-center justify-between text-muted-foreground">
                    <span>Скрипт</span>
                    <code className="rounded bg-secondary/50 px-2 py-1 text-xs text-secondary-foreground">
                      python -m scripts.{site.script ?? 'unknown'}
                    </code>
                  </div>
                  <div className="flex items-center justify-between text-muted-foreground">
                    <span>Последний экспорт</span>
                    <span>{lastExport}</span>
                  </div>
                  <div className="flex items-center justify-between text-muted-foreground">
                    <span>Статус</span>
                    <span className="capitalize">{site.status.split('_').join(' ')}</span>
                  </div>
                  <div className="flex items-center justify-between text-muted-foreground">
                    <span>Карта</span>
                    <Badge variant={mapStatusMeta[site.mapStatus]?.variant ?? 'outline'}>
                      {mapStatusMeta[site.mapStatus]?.label ?? site.mapStatus}
                    </Badge>
                  </div>
                  {site.mapFile && (
                    <div className="flex flex-col gap-1 text-xs text-muted-foreground">
                      <span className="font-mono text-[11px]">{site.mapFile}</span>
                      <span>Обновлено: {site.mapLastModified ? new Date(site.mapLastModified).toLocaleString('ru-RU') : '—'}</span>
                    </div>
                  )}
                </CardContent>
              </Card>
            </Link>
          );
        })}
        {filteredSites.length === 0 && (
          <div className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
            Нет сайтов, соответствующих запросу.
          </div>
        )}
      </div>
    </section>
  );
}
