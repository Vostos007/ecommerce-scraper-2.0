'use client';

import { useQuery } from '@tanstack/react-query';

import { DownloadCenter } from '@/components/DownloadCenter';
import { ProxyStats } from '@/components/ProxyStats';
import { SiteSelector } from '@/components/SiteSelector';
import { SummaryDashboard } from '@/components/SummaryDashboard';
import type { SiteSummary } from '@/lib/sites';
import { getSites } from '@/lib/api';

function LoadingSkeleton() {
  return (
    <div className="grid gap-3">
      <div className="h-24 rounded-lg bg-muted/20" />
      <div className="h-24 rounded-lg bg-muted/20" />
      <div className="h-24 rounded-lg bg-muted/20" />
    </div>
  );
}

export default function DashboardPage() {
  const { data, error, isLoading, isFetching, refetch } = useQuery<SiteSummary[], Error>({
    queryKey: ['sites'],
    queryFn: getSites,
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false
  });

  return (
    <div className="flex flex-col gap-10">
      <section className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-3xl font-bold text-foreground">UI Dashboard</h1>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Управляйте Python экспортерами: запускайте процессы, контролируйте логи, загружайте карты, собирайте
            сводные отчёты и следите за состоянием прокси инфраструктуры.
          </p>
        </div>
        <button
          type="button"
          className="text-xs text-muted-foreground hover:text-foreground"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          {isFetching ? 'Обновляем…' : 'Обновить список'}
        </button>
      </section>

      <SummaryDashboard />

      <DownloadCenter />

      <section className="space-y-4">
        <header className="flex items-center justify-between">
          <h2 className="text-2xl font-semibold">Площадки</h2>
          {error && (
            <span className="text-sm text-rose-300">Не удалось загрузить список сайтов: {error.message}</span>
          )}
        </header>

        {isLoading && !data && <LoadingSkeleton />}

        {data && data.length > 0 && <SiteSelector sites={data} />}
      </section>

      <section className="space-y-4">
        <h2 className="text-2xl font-semibold">Прокси инфраструктура</h2>
        <ProxyStats />
      </section>
    </div>
  );
}
