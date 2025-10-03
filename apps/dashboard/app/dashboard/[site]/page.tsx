import Link from 'next/link';
import { headers } from 'next/headers';
import { notFound } from 'next/navigation';

import { SiteDashboardClient } from '@/components/SiteDashboardClient';
import { SiteSectionNav, type SiteSectionDefinition } from '@/components/SiteSectionNav';
import type { SiteSummary } from '@/lib/sites';

interface SitePageProps {
  params: Promise<{ site: string }>;
}

async function resolveApiOrigin(): Promise<string> {
  const incomingHeaders = await headers();
  const forwardedProto =
    incomingHeaders.get('x-forwarded-proto') ?? incomingHeaders.get('protocol') ?? 'http';
  const forwardedHost = incomingHeaders.get('x-forwarded-host') ?? incomingHeaders.get('host');

  const configuredOrigin =
    process.env.NEXT_PUBLIC_DASHBOARD_ORIGIN ??
    process.env.DASHBOARD_ORIGIN ??
    process.env.NEXT_PUBLIC_APP_URL ??
    null;

  if (forwardedHost) {
    const candidate = `${forwardedProto}://${forwardedHost}`;
    if (!configuredOrigin) {
      return candidate;
    }

    try {
      const configuredUrl = new URL(configuredOrigin);
      const sameHost = configuredUrl.host === forwardedHost;
      if (sameHost) {
        return configuredUrl.origin;
      }

      if (process.env.NODE_ENV === 'development') {
        // В дев-режиме предпочитаем реальный хост, чтобы не спотыкаться на битых .env значениях.
        return candidate;
      }

      return configuredUrl.origin;
    } catch {
      // если origin некорректен, игнорируем его
      return candidate;
    }
  }

  if (configuredOrigin) {
    try {
      return new URL(configuredOrigin).origin;
    } catch {
      // ignore malformed url
    }
  }

  if (process.env.VERCEL_URL) {
    return `https://${process.env.VERCEL_URL}`;
  }
  if (process.env.PORT) {
    return `http://localhost:${process.env.PORT}`;
  }
  return 'http://localhost:3000';
}

async function fetchSiteSummary(site: string): Promise<SiteSummary | null> {
  const origin = await resolveApiOrigin();
  const response = await fetch(`${origin}/api/sites/${encodeURIComponent(site)}`, {
    cache: 'no-store'
  });
  if (!response.ok) {
    return null;
  }
  return (await response.json()) as SiteSummary;
}

export default async function SiteDashboardPage({ params }: SitePageProps) {
  const { site } = await params;
  const summary = await fetchSiteSummary(site);

  if (!summary) {
    notFound();
  }

  const sections: SiteSectionDefinition[] = [
    { id: 'summary', label: 'Сводка', target: '#summary' },
    { id: 'export', label: 'Экспорт', target: '#export' },
    { id: 'logs', label: 'Логи', target: '#logs' }
  ];

  return (
    <div className="flex flex-col gap-8">
      <div className="space-y-3">
        <nav className="text-xs text-muted-foreground" aria-label="Навигация по разделам">
          <ol className="flex items-center gap-2">
            <li>
              <Link href="/dashboard" className="transition hover:text-foreground">
                Обзор
              </Link>
            </li>
            <li aria-hidden="true">/</li>
            <li>{summary.name}</li>
          </ol>
        </nav>
        <div className="space-y-2">
          <h1 className="text-3xl font-bold text-foreground">{summary.name}</h1>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Управление экспортом для домена{' '}
            <strong className="font-semibold text-foreground">{summary.domain}</strong>. Скрипт:{' '}
            <code className="rounded bg-secondary/40 px-2 py-1 text-xs">python -u -m scripts.{summary.script}</code>
          </p>
        </div>
      </div>
      <SiteSectionNav sections={sections} />
      <SiteDashboardClient site={summary.domain} />
    </div>
  );
}
