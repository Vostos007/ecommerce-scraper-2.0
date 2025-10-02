'use client';

import { Fragment } from 'react';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { useSummaryMetrics } from '@/hooks/useSummaryMetrics';
import type { SiteSummaryMetrics } from '@/lib/validations';

const numberFormatter = new Intl.NumberFormat('ru-RU');
const percentFormatter = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 1 });

function formatNumber(value: number | null | undefined) {
  if (typeof value !== 'number' || Number.isNaN(value)) {
    return '—';
  }
  return numberFormatter.format(value);
}

function renderStatus(status: SiteSummaryMetrics['status']) {
  switch (status) {
    case 'ok':
      return <Badge variant="success">OK</Badge>;
    case 'missing':
      return <Badge variant="warning">Нет данных</Badge>;
    case 'error':
    default:
      return <Badge variant="error">Ошибка</Badge>;
  }
}

export function SummaryDashboard() {
  const { data, error, isLoading, totals, topSites, sitesWithIssues, refetch } = useSummaryMetrics();

  if (isLoading) {
    return (
      <Card className="border-dashed">
        <CardHeader>
          <CardTitle>Сводные метрики</CardTitle>
          <CardDescription>Загружаем активность экспорта…</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
            {Array.from({ length: 4 }).map((_, index) => (
              <div key={index} className="h-24 animate-pulse rounded-md bg-slate-800/40" />
            ))}
          </div>
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card className="border-rose-700/60 bg-rose-950/20">
        <CardHeader>
          <CardTitle>Не удалось получить сводку</CardTitle>
          <CardDescription>{error}</CardDescription>
        </CardHeader>
        <CardContent>
          <button
            type="button"
            className="text-sm underline decoration-dotted"
            onClick={() => refetch()}
          >
            Повторить попытку
          </button>
        </CardContent>
      </Card>
    );
  }

  if (!data || !totals) {
    return null;
  }

  const pricedRatio = totals.totalProducts > 0
    ? Math.min(100, (totals.totalProductsWithPrice / totals.totalProducts) * 100)
    : 0;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader>
            <CardTitle>Всего товаров</CardTitle>
            <CardDescription>Количество уникальных SKU по всем площадкам</CardDescription>
          </CardHeader>
          <CardContent className="pt-2">
            <p className="text-3xl font-semibold">{formatNumber(totals.totalProducts)}</p>
            <div className="mt-4 space-y-2">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>С ценой</span>
                <span>{formatNumber(totals.totalProductsWithPrice)}</span>
              </div>
              <Progress value={pricedRatio} aria-label="Доля товаров с ценой" />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Складской остаток</CardTitle>
            <CardDescription>Сумма stock по всем позициям</CardDescription>
          </CardHeader>
          <CardContent className="pt-2">
            <p className="text-3xl font-semibold">{formatNumber(Math.round(totals.totalStock))}</p>
            <p className="text-xs text-muted-foreground mt-2">
              Сайтов в мониторинге: {formatNumber(totals.totalSites)}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Вариации</CardTitle>
            <CardDescription>SKU с вариантами и общее число вариаций</CardDescription>
          </CardHeader>
          <CardContent className="pt-2">
            <p className="text-3xl font-semibold">{formatNumber(totals.totalVariations)}</p>
            <p className="text-xs text-muted-foreground mt-2">
              Товаров с вариациями: {formatNumber(totals.totalProductsWithVariations)}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Успешность экспортов</CardTitle>
            <CardDescription>Средний success rate по активным площадкам</CardDescription>
          </CardHeader>
          <CardContent className="pt-2">
            <p className="text-3xl font-semibold">
              {totals.averageSuccessRate !== null
                ? `${percentFormatter.format(totals.averageSuccessRate)}%`
                : '—'}
            </p>
            {sitesWithIssues.length > 0 ? (
              <p className="mt-2 text-xs text-amber-200">
                Требуется внимание: {sitesWithIssues.join(', ')}
              </p>
            ) : (
              <p className="mt-2 text-xs text-muted-foreground">Все экспорты стабильны</p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Ведущие площадки</CardTitle>
          <CardDescription>Топ-5 по количеству активных товаров</CardDescription>
        </CardHeader>
        <CardContent className="pt-0">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead className="bg-slate-900/40 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-left">Сайт</th>
                  <th className="px-4 py-2 text-right">Товары</th>
                  <th className="px-4 py-2 text-right">С вариациями</th>
                  <th className="px-4 py-2 text-right">Stock</th>
                  <th className="px-4 py-2 text-right">Success rate</th>
                  <th className="px-4 py-2 text-right">Статус</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {topSites.length === 0 && (
                  <tr>
                    <td colSpan={6} className="px-4 py-6 text-center text-muted-foreground">
                      Нет данных по площадкам
                    </td>
                  </tr>
                )}
                {topSites.map(({ domain, metrics }) => (
                  <tr key={domain} className="hover:bg-slate-900/40">
                    <td className="px-4 py-2 font-medium">
                      <div className="flex flex-col gap-1">
                        <span>{domain}</span>
                        {metrics.warnings && metrics.warnings.length > 0 && (
                          <span className="text-[11px] text-amber-200">
                            {metrics.warnings.join(', ')}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-2 text-right">{formatNumber(metrics.products)}</td>
                    <td className="px-4 py-2 text-right">{formatNumber(metrics.total_variations)}</td>
                    <td className="px-4 py-2 text-right">{formatNumber(Math.round(metrics.products_total_stock ?? 0))}</td>
                    <td className="px-4 py-2 text-right">
                      {typeof metrics.success_rate === 'number'
                        ? `${percentFormatter.format(metrics.success_rate)}%`
                        : '—'}
                    </td>
                    <td className="px-4 py-2 text-right">{renderStatus(metrics.status)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {sitesWithIssues.length > 0 && (
        <Card className="border-amber-700/60 bg-amber-900/20">
          <CardHeader>
            <CardTitle>Найденные проблемы</CardTitle>
            <CardDescription>
              Площадки с ошибками экспортов или неполными данными
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {sitesWithIssues.map((domain) => {
              const metrics = data.sites[domain];
              const issues = metrics?.warnings ?? metrics?.errors ?? [];
              return (
                <Fragment key={domain}>
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <p className="font-semibold">{domain}</p>
                      {Array.isArray(issues) && issues.length > 0 ? (
                        <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-amber-200">
                          {issues.map((warning, idx) => (
                            <li key={idx}>{warning}</li>
                          ))}
                        </ul>
                      ) : (
                        <p className="text-sm text-amber-200">
                          Требуется проверка экспортов и свежих отчётов
                        </p>
                      )}
                    </div>
                    {renderStatus(metrics?.status ?? 'error')}
                  </div>
                </Fragment>
              );
            })}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
