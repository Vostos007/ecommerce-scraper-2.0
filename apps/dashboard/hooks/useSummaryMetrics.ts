'use client';

import { useMemo } from 'react';

import { useQuery } from '@tanstack/react-query';

import { getSummaryMetrics } from '@/lib/api';
import type { SiteSummaryMetrics, SummaryResponse } from '@/lib/validations';

interface ComputedTotals {
  totalSites: number;
  totalProducts: number;
  totalProductsWithPrice: number;
  totalStock: number;
  totalVariations: number;
  totalProductsWithVariations: number;
  averageSuccessRate: number | null;
}

export function useSummaryMetrics() {
  const query = useQuery<SummaryResponse, Error>({
    queryKey: ['summary-metrics'],
    queryFn: getSummaryMetrics,
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
    retry: 2
  });

  const summary = query.data ?? null;

  const derived = useMemo(() => {
    if (!summary) {
      return {
        totals: null,
        topSites: [] as Array<{ domain: string; metrics: SiteSummaryMetrics }>,
        sitesWithIssues: [] as string[]
      };
    }

    const sitesEntries = Object.entries(summary.sites);
    const topSites = [...sitesEntries]
      .sort(([, a], [, b]) => (b.products ?? 0) - (a.products ?? 0))
      .slice(0, 5)
      .map(([domain, metrics]) => ({ domain, metrics }));

    const sitesWithIssues = sitesEntries
      .filter(([, metrics]) => metrics.status !== 'ok')
      .map(([domain]) => domain);

    const totals: ComputedTotals = {
      totalSites: summary.totals.total_sites,
      totalProducts: summary.totals.total_products,
      totalProductsWithPrice: summary.totals.total_products_with_price,
      totalStock: summary.totals.total_stock,
      totalVariations: summary.totals.total_variations,
      totalProductsWithVariations: summary.totals.total_products_with_variations ?? 0,
      averageSuccessRate:
        typeof summary.totals.average_success_rate === 'number'
          ? summary.totals.average_success_rate
          : null
    };

    return { totals, topSites, sitesWithIssues };
  }, [summary]);

  return {
    data: summary,
    error: query.error?.message ?? null,
    isLoading: query.isLoading,
    refetch: query.refetch,
    totals: derived.totals,
    topSites: derived.topSites,
    sitesWithIssues: derived.sitesWithIssues
  };
}
