import { promises as fs } from 'node:fs';
import { NextResponse } from 'next/server';
import { z } from 'zod';

import { resolveRepoPath } from '@/lib/paths';
import { siteSummaryMetricsSchema, summaryResponseSchema } from '@/lib/validations';
import { withApiMetrics } from '@/lib/metrics';

export const runtime = 'nodejs';

const SUMMARY_PATH = resolveRepoPath('reports', 'firecrawl_baseline_summary.json');
const CACHE_TTL_MS = 10 * 60 * 1000;

const METRIC_KEYS = [
  'status',
  'export_file',
  'products',
  'products_with_price',
  'products_with_stock_field',
  'products_in_stock_true',
  'products_total_stock',
  'products_with_variations',
  'total_variations',
  'variations_total_stock',
  'variations_in_stock_true',
  'success_rate',
  'errors',
  'warnings',
  'updated_at'
] satisfies Array<keyof z.infer<typeof siteSummaryMetricsSchema>>;

interface CacheEntry {
  data: z.infer<typeof summaryResponseSchema>;
  expiresAt: number;
  mtimeMs: number;
}

let cache: CacheEntry | null = null;

async function loadSummary() {
  const stat = await fs.stat(SUMMARY_PATH);
  const raw = await fs.readFile(SUMMARY_PATH, 'utf-8');
  const json = JSON.parse(raw) as unknown;

  if (!json || typeof json !== 'object' || Array.isArray(json)) {
    throw new Error('Некорректная структура summary файла');
  }

  const summaries: Record<string, z.infer<typeof siteSummaryMetricsSchema>> = {};

  for (const [domain, metrics] of Object.entries(json as Record<string, unknown>)) {
    if (!metrics || typeof metrics !== 'object' || Array.isArray(metrics)) {
      console.warn('[dashboard] пропущена запись summary: некорректный формат', { domain });
      continue;
    }

    const candidate: Record<string, unknown> = {};
    for (const key of METRIC_KEYS) {
      if (key in metrics) {
        candidate[key] = (metrics as Record<string, unknown>)[key];
      }
    }

    const result = siteSummaryMetricsSchema.safeParse(candidate);
    if (result.success) {
      summaries[domain] = result.data;
    } else {
      console.warn('[dashboard] summary metrics validation error', {
        domain,
        issues: result.error.issues.map((issue) => issue.message)
      });
    }
  }

  const totals = Object.values(summaries).reduce(
    (acc, metrics) => {
      acc.total_products += metrics.products ?? 0;
      acc.total_products_with_price += metrics.products_with_price ?? 0;
      acc.total_stock += metrics.products_total_stock ?? 0;
      acc.total_variations += metrics.total_variations ?? 0;
      acc.total_products_with_variations += metrics.products_with_variations ?? 0;
      if (typeof metrics.success_rate === 'number') {
        acc.successRateSum += metrics.success_rate;
        acc.successRateCount += 1;
      }
      return acc;
    },
    {
      total_products: 0,
      total_products_with_price: 0,
      total_stock: 0,
      total_variations: 0,
      total_products_with_variations: 0,
      successRateSum: 0,
      successRateCount: 0
    }
  );

  const totalSites = Object.keys(summaries).length;
  const response = {
    sites: summaries,
    totals: {
      total_sites: totalSites,
      total_products: totals.total_products,
      total_products_with_price: totals.total_products_with_price,
      total_stock: totals.total_stock,
      total_variations: totals.total_variations,
      total_products_with_variations: totals.total_products_with_variations,
      average_success_rate:
        totals.successRateCount > 0 ? totals.successRateSum / totals.successRateCount : undefined
    },
    generated_at: stat.mtime.toISOString()
  } as const;

  cache = {
    data: summaryResponseSchema.parse(response),
    expiresAt: Date.now() + CACHE_TTL_MS,
    mtimeMs: stat.mtimeMs
  };

  return cache.data;
}

const handler = async () => {
  try {
    const stat = await fs.stat(SUMMARY_PATH);
    if (cache && cache.mtimeMs === stat.mtimeMs && cache.expiresAt > Date.now()) {
      return NextResponse.json(cache.data, { headers: { 'Cache-Control': 'private, max-age=60' } });
    }
    const data = await loadSummary();
    return NextResponse.json(data, { headers: { 'Cache-Control': 'private, max-age=60' } });
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') {
      return NextResponse.json({ error: 'Файл summary не найден' }, { status: 404 });
    }
    return NextResponse.json({ error: 'Не удалось прочитать summary' }, { status: 500 });
  }
};

export const GET = withApiMetrics('summary', handler);
