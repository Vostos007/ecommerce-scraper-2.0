'use client';

import { FormEvent, useMemo, useState } from 'react';

import { useProxyPool } from '@/hooks/useProxyPool';
import { useProxyStats } from '@/hooks/useProxyStats';
import { cn } from '@/lib/utils';

import { Button } from './ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './ui/card';
import { Badge } from './ui/badge';

function normalizeInput(value: string): string[] {
  return value
    .split(/\r?\n|,|\s+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function maskProxyUrl(url: string, username: string | null, password: string | null, scheme: string): string {
  if (!username) {
    return url;
  }
  const maskedPassword = password ? '••••••' : '';
  return `${scheme}://${username}${maskedPassword ? `:${maskedPassword}` : ''}@${url.split('@').pop() ?? ''}`;
}

export function ProxyManager() {
  const { data, isLoading, isFetching, error, refetch, addProxies, isAdding, addError, removeProxies, isRemoving, removeError } =
    useProxyPool();
  const [input, setInput] = useState('');
  const [replace, setReplace] = useState(false);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);

  const proxies = useMemo(() => data?.proxies ?? [], [data]);
  const updatedAt = data?.updatedAt ? new Date(data.updatedAt) : null;
  const { data: statsData } = useProxyStats();
  const freshnessMap = useMemo(() => {
    const map = new Map<string, { status: 'active' | 'stale' | 'inactive'; last_seen: string | null; age_hours: number | null }>();
    statsData?.proxy_freshness?.forEach((entry) => {
      map.set(entry.canonical, {
        status: entry.status,
        last_seen: entry.last_seen ?? null,
        age_hours: entry.age_hours ?? null
      });
    });
    return map;
  }, [statsData]);

  const statusLabels: Record<'active' | 'stale' | 'inactive', string> = {
    active: 'Актуален',
    stale: 'Нужен апдейт',
    inactive: 'Нет активности'
  };

  const statusVariants: Record<'active' | 'stale' | 'inactive', 'success' | 'warning' | 'error' | 'outline'> = {
    active: 'success',
    stale: 'warning',
    inactive: 'error'
  };

  const formatLastSeen = (iso: string | null, ageHours: number | null): string => {
    if (!iso || ageHours == null) {
      return 'Нет данных';
    }
    if (ageHours < 1) {
      return 'Менее часа назад';
    }
    if (ageHours < 24) {
      return `${Math.round(ageHours)} ч назад`;
    }
    const days = Math.floor(ageHours / 24);
    if (days < 7) {
      return `${days} дн назад`;
    }
    return new Date(iso).toLocaleString('ru-RU');
  };

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setValidationError(null);
    setSuccessMessage(null);

    const entries = normalizeInput(input);
    if (entries.length === 0) {
      setValidationError('Добавьте хотя бы один прокси (формат: scheme://user:pass@host:port).');
      return;
    }

    try {
      const result = await addProxies({ proxies: entries, replace });
      setSuccessMessage(
        result.skipped.length
          ? `Добавлено ${result.added.length}, пропущено ${result.skipped.length} (уже существовали).`
          : `Добавлено ${result.added.length} прокси.`
      );
      if (replace || result.added.length > 0) {
        setInput('');
      }
      await refetch();
    } catch (err) {
      setValidationError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleRemove(url: string) {
    setSuccessMessage(null);
    setValidationError(null);
    try {
      const result = await removeProxies([url]);
      if (result.missing.length) {
        setValidationError('Прокси не найден, возможно конфиг уже обновлён.');
      } else {
        setSuccessMessage('Прокси удалён.');
      }
      await refetch();
    } catch (err) {
      setValidationError(err instanceof Error ? err.message : String(err));
    }
  }

  const isBusy = isAdding || isRemoving;
  const globalError = error ?? addError ?? removeError;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Управление прокси</CardTitle>
        <CardDescription>
          Добавляйте или удаляйте прокси из пула datacenter. Изменения автоматически попадают в <code>config/proxy/proxy_pools.yml</code> и связанные файлы.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <form className="space-y-3" onSubmit={handleSubmit}>
          <div className="space-y-2">
            <label htmlFor="proxy-input" className="text-sm font-medium text-foreground">
              Прокси (по одному в строке)
            </label>
            <textarea
              id="proxy-input"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="https://user:pass@203.0.113.10:8000"
              disabled={isBusy}
              className={cn(
                'min-h-[120px] w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono leading-5 text-foreground shadow-sm transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50',
                validationError ? 'border-rose-500/60' : ''
              )}
            />
            <p className="text-xs text-muted-foreground">
              Поддерживаются схемы <code>http</code>, <code>https</code>, <code>socks5</code>, <code>socks4</code>.
            </p>
          </div>
          <label className="flex items-center gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              className="h-4 w-4 rounded border border-border"
              checked={replace}
              onChange={(event) => setReplace(event.target.checked)}
              disabled={isBusy}
            />
            Заменить существующий список полностью
          </label>
          <div className="flex items-center gap-2">
            <Button type="submit" disabled={isBusy}>
              {isAdding ? 'Сохраняем…' : replace ? 'Заменить список' : 'Добавить прокси'}
            </Button>
            <Button type="button" variant="ghost" onClick={() => refetch()} disabled={isFetching || isBusy}>
              {isFetching ? 'Обновляем…' : 'Обновить состояние'}
            </Button>
          </div>
          {validationError && (
            <div className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
              {validationError}
            </div>
          )}
          {successMessage && (
            <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-100">
              {successMessage}
            </div>
          )}
          {globalError && !validationError && (
            <div className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
              {globalError.message}
            </div>
          )}
        </form>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-foreground">
              Текущий пул ({proxies.length})
            </h3>
            <span className="text-xs text-muted-foreground">
              {updatedAt ? `Обновлено: ${updatedAt.toLocaleString('ru-RU')}` : 'Время обновления неизвестно'}
            </span>
          </div>
          {isLoading && (
            <div className="rounded-md border border-border bg-secondary/10 px-3 py-4 text-sm text-muted-foreground">
              Загружаем список прокси…
            </div>
          )}
          {!isLoading && proxies.length === 0 && (
            <div className="rounded-md border border-border bg-secondary/10 px-3 py-4 text-sm text-muted-foreground">
              Пул прокси пуст. Добавьте записи, чтобы использовать datacenter пул.
            </div>
          )}
          {!isLoading && proxies.length > 0 && (
            <div className="overflow-hidden rounded-lg border border-border">
              <table className="w-full table-fixed text-left text-sm">
                <thead className="bg-secondary/20 text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="w-1/2 px-3 py-2 font-medium">Прокси</th>
                    <th className="w-1/6 px-3 py-2 font-medium">Схема</th>
                    <th className="w-1/6 px-3 py-2 font-medium">Статус</th>
                    <th className="w-1/6 px-3 py-2 font-medium text-right">Действия</th>
                  </tr>
                </thead>
                <tbody>
                  {proxies.map((proxy) => {
                    const statusEntry = freshnessMap.get(proxy.url);
                    const rowClass = cn(
                      'border-t border-border/60',
                      statusEntry && statusEntry.status !== 'active' ? 'bg-amber-500/5' : ''
                    );
                    return (
                      <tr key={proxy.url} className={rowClass}>
                      <td className="px-3 py-2 font-mono text-xs text-foreground">
                        <span title={proxy.url}>
                          {maskProxyUrl(proxy.url, proxy.username, proxy.password, proxy.scheme)}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">{proxy.scheme}</td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">
                        {statusEntry ? (
                          <div className="flex flex-col gap-1">
                            <Badge variant={statusVariants[statusEntry.status]} className="w-fit text-[11px] uppercase tracking-wide">
                              {statusLabels[statusEntry.status]}
                            </Badge>
                            <span className="text-[11px] text-muted-foreground/80">
                              {formatLastSeen(statusEntry.last_seen, statusEntry.age_hours)}
                            </span>
                          </div>
                        ) : (
                          <span className="text-[11px] text-muted-foreground/70">Нет данных</span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="text-xs text-rose-400 hover:text-rose-200"
                          disabled={isBusy}
                          onClick={() => void handleRemove(proxy.url)}
                        >
                          Удалить
                        </Button>
                      </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
