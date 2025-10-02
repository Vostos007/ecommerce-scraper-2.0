import fs from 'node:fs/promises';
import path from 'node:path';
import { NextResponse } from 'next/server';
import { z } from 'zod';

import { resolveRepoPath } from '@/lib/paths';
import { sanitizeSite } from '@/lib/sites';
import { siteSummaryMetricsSchema, summaryResponseSchema, type SummaryResponse } from '@/lib/validations';
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

const EXPORT_FILENAME = 'latest.json';
const STOCK_KEYS = ['stock_quantity', 'stock', 'inventory', 'available', 'quantity'] as const;

interface CacheEntry {
  data: z.infer<typeof summaryResponseSchema>;
  expiresAt: number;
  mtimeMs: number;
}

let cache: CacheEntry | null = null;

async function loadConfiguredDomains(): Promise<string[]> {
  try {
    const raw = await fs.readFile(resolveRepoPath('config', 'sites.json'), 'utf-8');
    const parsed = JSON.parse(raw) as { sites?: Array<{ domain?: unknown }> };
    const domains = new Set<string>();
    for (const entry of parsed.sites ?? []) {
      const candidate = sanitizeSite(entry.domain);
      if (candidate) {
        domains.add(candidate);
      }
    }
    if (domains.size > 0) {
      return Array.from(domains);
    }
  } catch (error) {
    console.warn('[dashboard] не удалось прочитать config/sites.json', {
      error: error instanceof Error ? error.message : String(error)
    });
  }

  try {
    const siteDir = resolveRepoPath('data', 'sites');
    const entries = await fs.readdir(siteDir, { withFileTypes: true });
    const domains: string[] = [];
    for (const entry of entries) {
      if (!entry.isDirectory()) {
        continue;
      }
      const candidate = sanitizeSite(entry.name);
      if (candidate) {
        domains.push(candidate);
      }
    }
    return domains;
  } catch (error) {
    console.warn('[dashboard] не удалось перечислить data/sites', {
      error: error instanceof Error ? error.message : String(error)
    });
    return [];
  }
}

function coerceNumber(value: unknown): number | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string') {
    const normalized = value.trim().replace(/\s+/g, '').replace(',', '.');
    if (!normalized) {
      return null;
    }
    const parsed = Number.parseFloat(normalized);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return null;
}

interface ProductAggregates {
  products: number;
  products_with_price: number;
  products_with_stock_field: number;
  products_in_stock_true: number;
  products_total_stock: number;
  products_with_variations: number;
  total_variations: number;
  variations_total_stock: number;
  variations_in_stock_true: number;
}

function aggregateProducts(products: unknown[]): ProductAggregates {
  const aggregates: ProductAggregates = {
    products: 0,
    products_with_price: 0,
    products_with_stock_field: 0,
    products_in_stock_true: 0,
    products_total_stock: 0,
    products_with_variations: 0,
    total_variations: 0,
    variations_total_stock: 0,
    variations_in_stock_true: 0
  };

  for (const item of products) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      continue;
    }
    const record = item as Record<string, unknown>;
    aggregates.products += 1;

    const priceValue = coerceNumber(record.price);
    if (priceValue !== null) {
      aggregates.products_with_price += 1;
    }

    if (record.in_stock === true) {
      aggregates.products_in_stock_true += 1;
    }

    for (const key of STOCK_KEYS) {
      const candidate = coerceNumber(record[key]);
      if (candidate !== null) {
        aggregates.products_with_stock_field += 1;
        aggregates.products_total_stock += candidate;
        break;
      }
    }

    const variations = Array.isArray(record.variations) ? record.variations : [];
    if (variations.length > 0) {
      aggregates.products_with_variations += 1;
    }

    for (const variation of variations) {
      if (!variation || typeof variation !== 'object' || Array.isArray(variation)) {
        continue;
      }
      const variationRecord = variation as Record<string, unknown>;
      aggregates.total_variations += 1;
      const variationStock = coerceNumber(variationRecord.stock);
      if (variationStock !== null) {
        aggregates.variations_total_stock += variationStock;
      }
      if (variationRecord.in_stock === true) {
        aggregates.variations_in_stock_true += 1;
      }
    }
  }

  aggregates.products_total_stock = Number(aggregates.products_total_stock.toFixed(2));
  aggregates.variations_total_stock = Number(aggregates.variations_total_stock.toFixed(2));

  return aggregates;
}

async function loadDomainSummary(domain: string): Promise<{
  metrics: z.infer<typeof siteSummaryMetricsSchema>;
  generatedAt?: string;
}> {
  const exportPath = resolveRepoPath('data', 'sites', domain, 'exports', EXPORT_FILENAME);
  const baseMetrics = {
    status: 'missing' as const,
    export_file: EXPORT_FILENAME,
    products: 0,
    products_with_price: 0,
    products_with_stock_field: 0,
    products_in_stock_true: 0,
    products_total_stock: 0,
    products_with_variations: 0,
    total_variations: 0,
    variations_total_stock: 0,
    variations_in_stock_true: 0
  } satisfies Partial<z.infer<typeof siteSummaryMetricsSchema>>;

  try {
    const raw = await fs.readFile(exportPath, 'utf-8');
    const parsed = JSON.parse(raw) as { products?: unknown; generated_at?: unknown };
    const products = Array.isArray(parsed.products) ? parsed.products : [];
    const aggregates = aggregateProducts(products);
    const updatedAtRaw = typeof parsed.generated_at === 'string' ? parsed.generated_at : null;
    const fileStat = await fs.stat(exportPath);
    const updatedAt = updatedAtRaw && !Number.isNaN(Date.parse(updatedAtRaw))
      ? updatedAtRaw
      : fileStat.mtime.toISOString();

    const metricsCandidate: Record<string, unknown> = {
      ...baseMetrics,
      ...aggregates,
      status: aggregates.products > 0 ? 'ok' : 'missing',
      export_file: EXPORT_FILENAME,
      updated_at: updatedAt
    };

    for (const key of Object.keys(metricsCandidate)) {
      const value = (metricsCandidate as Record<string, unknown>)[key];
      if (value === null) {
        delete (metricsCandidate as Record<string, unknown>)[key];
      }
    }

    const metrics = siteSummaryMetricsSchema.parse(metricsCandidate);
    if (metrics.updated_at) {
      return { metrics, generatedAt: metrics.updated_at };
    }
    return { metrics };
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === 'ENOENT') {
      const metrics = siteSummaryMetricsSchema.parse({
        ...baseMetrics,
        status: 'missing',
        errors: [`Экспорт ${EXPORT_FILENAME} не найден`]
      });
      return { metrics };
    }

    const metrics = siteSummaryMetricsSchema.parse({
      ...baseMetrics,
      status: 'error',
      errors: [error instanceof Error ? error.message : 'Не удалось прочитать экспорт']
    });
    return { metrics };
  }
}

function computeTotals(values: Record<string, z.infer<typeof siteSummaryMetricsSchema>>) {
  return Object.values(values).reduce(
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
}

async function generateSummaryFromExports(): Promise<SummaryResponse> {
  const domains = await loadConfiguredDomains();
  const summaries: Record<string, z.infer<typeof siteSummaryMetricsSchema>> = {};
  let newestTimestamp: number | null = null;

  for (const domain of domains) {
    const { metrics, generatedAt } = await loadDomainSummary(domain);
    summaries[domain] = metrics;
    if (generatedAt) {
      const timestamp = Date.parse(generatedAt);
      if (Number.isFinite(timestamp)) {
        newestTimestamp = newestTimestamp === null ? timestamp : Math.max(newestTimestamp, timestamp);
      }
    }
  }

  const totals = computeTotals(summaries);
  const generatedTimestamp = newestTimestamp ?? Date.now();

  const payload = {
    sites: summaries,
    totals: {
      total_sites: Object.keys(summaries).length,
      total_products: totals.total_products,
      total_products_with_price: totals.total_products_with_price,
      total_stock: totals.total_stock,
      total_variations: totals.total_variations,
      total_products_with_variations:
        totals.total_products_with_variations > 0 ? totals.total_products_with_variations : undefined,
      average_success_rate:
        totals.successRateCount > 0 ? totals.successRateSum / totals.successRateCount : undefined
    },
    generated_at: new Date(generatedTimestamp).toISOString()
  } satisfies SummaryResponse;

  return summaryResponseSchema.parse(payload);
}

async function persistSummaryFile(summary: SummaryResponse): Promise<void> {
  const targetDir = path.dirname(SUMMARY_PATH);
  await fs.mkdir(targetDir, { recursive: true });
  await fs.writeFile(SUMMARY_PATH, JSON.stringify(summary, null, 2), 'utf-8');
}

async function loadSummaryFromDisk(stat: Awaited<ReturnType<typeof fs.stat>>): Promise<SummaryResponse> {
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
        const value = (metrics as Record<string, unknown>)[key];
        if (value !== null && value !== undefined) {
          candidate[key] = value;
        }
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

  const totals = computeTotals(summaries);

  const payload: SummaryResponse = {
    sites: summaries,
    totals: {
      total_sites: Object.keys(summaries).length,
      total_products: totals.total_products,
      total_products_with_price: totals.total_products_with_price,
      total_stock: totals.total_stock,
      total_variations: totals.total_variations,
      total_products_with_variations:
        totals.total_products_with_variations > 0 ? totals.total_products_with_variations : undefined,
      average_success_rate:
        totals.successRateCount > 0 ? totals.successRateSum / totals.successRateCount : undefined
    },
    generated_at: stat.mtime.toISOString()
  };

  return summaryResponseSchema.parse(payload);
}

async function getSummaryData(): Promise<SummaryResponse> {
  try {
    const stat = await fs.stat(SUMMARY_PATH);
    if (cache && cache.mtimeMs === stat.mtimeMs && cache.expiresAt > Date.now()) {
      return cache.data;
    }

    const data = await loadSummaryFromDisk(stat);
    cache = {
      data,
      expiresAt: Date.now() + CACHE_TTL_MS,
      mtimeMs: stat.mtimeMs
    };
    return data;
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code !== 'ENOENT') {
      console.warn('[dashboard] summary fallback to regenerated data', {
        error: error instanceof Error ? error.message : String(error)
      });
    }

    const generated = await generateSummaryFromExports();
    try {
      await persistSummaryFile(generated);
    } catch (persistError) {
      console.warn('[dashboard] не удалось сохранить summary', {
        error: persistError instanceof Error ? persistError.message : String(persistError)
      });
    }
    cache = {
      data: generated,
      expiresAt: Date.now() + CACHE_TTL_MS,
      mtimeMs: Date.now()
    };
    return generated;
  }
}

const handler = async () => {
  try {
    const data = await getSummaryData();
    return NextResponse.json(data, { headers: { 'Cache-Control': 'private, max-age=60' } });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : 'Не удалось сформировать summary' },
      { status: 500 }
    );
  }
};

export const GET = withApiMetrics('summary', handler);
