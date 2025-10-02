'use client';

import { useMemo } from 'react';

import { useProxyStats } from '@/hooks/useProxyStats';
import { cn, formatBytes } from '@/lib/utils';

import { Badge } from './ui/badge';
import { Button } from './ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './ui/card';
import { Progress } from './ui/progress';

const statusColor = (value: number, total: number) => {
  if (total === 0) {
    return 'text-muted-foreground';
  }
  const ratio = value / total;
  if (ratio > 0.7) {
    return 'text-emerald-400';
  }
  if (ratio > 0.4) {
    return 'text-amber-400';
  }
  return 'text-rose-400';
};

export function ProxyStats() {
  const { data, error, isLoading, refetch } = useProxyStats();

  const topCountries = useMemo(() => {
    if (!data?.proxy_countries) {
      return [] as Array<[string, number]>;
    }
    return Object.entries(data.proxy_countries)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
  }, [data]);

  const protocols = useMemo(() => {
    if (!data?.proxy_protocols) {
      return [] as Array<[string, number]>;
    }
    return Object.entries(data.proxy_protocols).sort((a, b) => b[1] - a[1]);
  }, [data]);

  const autoscale = data?.autoscale;
  const autoscaleStatus = data?.autoscale_status ?? autoscale?.status ?? 'sufficient';
  const optimalCount = autoscale?.optimal_proxy_count ?? data?.optimal_proxy_count ?? 0;
  const healthyCount = autoscale?.current_healthy ?? data?.healthy_proxies ?? 0;
  const recommendedPurchase = autoscale?.recommended_purchase ?? data?.recommended_purchase ?? 0;
  const purchaseEstimate = autoscale?.estimated_cost ?? data?.purchase_estimate ?? 0;
  const healthRatio = optimalCount > 0 ? Math.min(100, Math.round((healthyCount / optimalCount) * 100)) : healthyCount > 0 ? 100 : 0;
  const concurrency = data?.autoscale_concurrency ?? 0;
  const deficitValue = autoscale?.deficit;
  const deficitDisplay = deficitValue != null ? Math.max(0, deficitValue) : null;

  const autoscaleBadge: Record<string, { label: string; variant: 'default' | 'primary' | 'success' | 'warning' | 'error' | 'outline' }> = {
    sufficient: { label: 'Достаточно', variant: 'success' },
    warning: { label: 'Нехватка', variant: 'warning' },
    critical: { label: 'Критично', variant: 'error' }
  };

  return (
    <section className="space-y-4" data-testid="proxy-stats">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-foreground">Прокси статистика</h2>
          <p className="text-sm text-muted-foreground">
            Метрики собираются через <code>scripts.proxy_stats_export</code> и кешируются на 30 секунд.
          </p>
        </div>
        <button
          type="button"
          className="text-xs text-muted-foreground hover:text-foreground"
          onClick={() => refetch()}
          disabled={isLoading}
        >
          {isLoading ? 'Обновляем…' : 'Обновить'}
        </button>
      </div>

      {error && (
        <div
          data-testid="proxy-stats-error"
          className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-200"
        >
          Ошибка:{' '}
          {typeof error === 'string'
            ? error
            : (error as Error)?.message ?? String(error)}
        </div>
      )}

      {!error && (
        <div className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>Сводка</CardTitle>
              <CardDescription>
                {data?.generated_at
                  ? `Обновлено: ${new Date(data.generated_at).toLocaleString('ru-RU')}`
                  : 'Время обновления недоступно'}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <div>
                  <dt className="text-muted-foreground">Всего прокси</dt>
                  <dd className="text-xl font-semibold">{data?.total_proxies ?? '—'}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Активные прокси</dt>
                  <dd className={cn('text-xl font-semibold', data ? statusColor(data.active_proxies ?? 0, data.total_proxies ?? 1) : '')}>
                    {data?.active_proxies ?? '—'}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Успешность</dt>
                  <dd className="text-xl font-semibold">
                    {data?.success_rate != null ? `${data.success_rate.toFixed(1)}%` : '—'}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Неудачные прокси</dt>
                  <dd className="text-xl font-semibold text-amber-400">{data?.failed_proxies ?? '—'}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Сожжённые прокси</dt>
                  <dd className="text-xl font-semibold text-rose-400">{data?.burned_proxies ?? '—'}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Всего запросов</dt>
                  <dd className="text-xl font-semibold">{data?.total_requests ?? '—'}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Оптимальный пул</dt>
                  <dd className="text-xl font-semibold">{optimalCount || '—'}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Дефицит</dt>
                  <dd className="text-xl font-semibold text-amber-300">{deficitDisplay != null ? deficitDisplay : '—'}</dd>
                </div>
              </dl>

              {data?.premium_proxy_stats && (
                <div className="mt-6 grid gap-4 sm:grid-cols-2">
                  <div className="rounded-lg border border-border bg-secondary/10 p-4">
                    <div className="text-sm text-muted-foreground">Premium bandwidth</div>
                    <div className="text-lg font-semibold">
                      {data.premium_proxy_stats.bandwidth != null
                        ? formatBytes(data.premium_proxy_stats.bandwidth)
                        : '—'}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      Активные сессии: {data.premium_proxy_stats.active_sessions ?? '—'}
                    </div>
                  </div>
                  <div className="rounded-lg border border-border bg-secondary/10 p-4 space-y-1">
                    <div className="text-sm text-muted-foreground">Расходы</div>
                    <div className="text-lg font-semibold">
                      {data.premium_proxy_stats.cost != null
                        ? `${data.premium_proxy_stats.cost.toFixed(2)} ₽`
                        : '—'}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      Бюджет: {data.premium_proxy_stats.monthly_budget != null
                        ? `${data.premium_proxy_stats.monthly_budget.toFixed(2)} ₽`
                        : '—'}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      Остаток: {data.premium_proxy_stats.monthly_budget_remaining != null
                        ? `${data.premium_proxy_stats.monthly_budget_remaining.toFixed(2)} ₽`
                        : '—'}
                    </div>
                    {data.premium_proxy_stats.auto_purchase_enabled !== undefined && (
                      <div className="text-xs text-muted-foreground">
                        Автозакупка: {data.premium_proxy_stats.auto_purchase_enabled ? 'включена' : 'отключена'}
                      </div>
                    )}
                    {data.premium_proxy_stats.purchase_cooldown_remaining !== undefined && (
                      <div className="text-xs text-muted-foreground">
                        Cooldown: {data.premium_proxy_stats.purchase_cooldown_remaining} мин
                      </div>
                    )}
                  </div>
                </div>
              )}
            </CardContent>
          </Card>

          <div className="space-y-4">
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <CardTitle className="text-base">Авто масштабирование</CardTitle>
                  <Badge variant={autoscaleBadge[autoscaleStatus]?.variant ?? 'default'}>
                    {autoscaleBadge[autoscaleStatus]?.label ?? autoscaleStatus}
                  </Badge>
                </div>
                <CardDescription>
                  Цель: {optimalCount || '—'} прокси при concurrency {concurrency || '—'}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>Текущие здоровые прокси</span>
                    <span>
                      {healthyCount}/{optimalCount || '—'}
                    </span>
                  </div>
                  <Progress value={healthRatio} aria-label="Здоровые прокси" variant={autoscaleStatus === 'critical' ? 'error' : autoscaleStatus === 'warning' ? 'warning' : 'success'} />
                </div>

                {recommendedPurchase > 0 && (
                  <div className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">
                    <p>Рекомендуется докупить {recommendedPurchase} прокси</p>
                    {purchaseEstimate > 0 && (
                      <p className="text-xs text-amber-200">Примерная стоимость: {purchaseEstimate.toFixed(2)} ₽</p>
                    )}
                    <Button type="button" size="sm" variant="ghost" disabled className="h-7 border border-amber-500/40 text-amber-100 hover:text-amber-50">
                      Докупить прокси (скоро)
                    </Button>
                  </div>
                )}

                {data?.premium_proxy_stats?.auto_purchase_enabled && (
                  <p className="text-xs text-muted-foreground">
                    Автозакупка активна. Последняя закупка:{' '}
                    {data.premium_proxy_stats.last_purchase_time
                      ? new Date(data.premium_proxy_stats.last_purchase_time).toLocaleString('ru-RU')
                      : '—'}
                  </p>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-base">Протоколы</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                {protocols.length === 0 && <div className="text-sm text-muted-foreground">Нет данных</div>}
                {protocols.map(([protocol, count]) => (
                  <div key={protocol} className="flex items-center justify-between text-sm">
                    <span className="capitalize">{protocol}</span>
                    <span className="font-semibold">{count}</span>
                  </div>
                ))}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-base">Страны</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                {topCountries.length === 0 && <div className="text-sm text-muted-foreground">Нет данных</div>}
                {topCountries.map(([country, count]) => (
                  <div key={country} className="flex items-center justify-between text-sm">
                    <span>{country}</span>
                    <span className="font-semibold">{count}</span>
                  </div>
                ))}
              </CardContent>
            </Card>
          </div>
        </div>
      )}

      {data?.warnings && data.warnings.length > 0 && (
        <div className="space-y-2">
          {data.warnings.map((warning, index) => (
            <div
              key={index}
              className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-100"
            >
              <span className="text-amber-300">⚠️</span>
              <span>{warning}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
