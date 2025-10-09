import Link from 'next/link';
import { notFound } from 'next/navigation';

import { SiteDashboardClient } from '@/components/SiteDashboardClient';
import { SiteSectionNav, type SiteSectionDefinition } from '@/components/SiteSectionNav';
import { getSiteSummaries } from '@/lib/sites.server';
import { sanitizeSite, type SiteSummary } from '@/lib/sites';

interface SitePageProps {
  params: { site: string };
}

function getSiteSummary(site: string): SiteSummary | null {
  const normalized = sanitizeSite(site);
  if (!normalized) {
    return null;
  }
  const summaries = getSiteSummaries();
  return summaries.find((item) => item.domain === normalized) ?? null;
}

export default function SiteDashboardPage({ params }: SitePageProps) {
  const summary = getSiteSummary(params.site);

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
