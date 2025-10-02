'use client';

import { useMemo } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';

import { getActiveExportJob, getSites, type ActiveExportJob, type SiteSummary } from '@/lib/api';

export interface ActiveExportEntry {
  site: SiteSummary;
  job: ActiveExportJob;
}

export function useActiveExports() {
  const sitesQuery = useQuery<SiteSummary[], Error>({
    queryKey: ['sites'],
    queryFn: getSites,
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false
  });

  const sites = useMemo(() => sitesQuery.data ?? [], [sitesQuery.data]);

  const jobQueries = useQueries({
    queries: sites.map((site) => ({
      queryKey: ['export-active', site.domain],
      queryFn: () => getActiveExportJob(site.domain),
      enabled: sitesQuery.isSuccess,
      refetchInterval: 5000,
      retry: false
    }))
  });

  const activeJobs = useMemo(() => {
    return jobQueries
      .map((query, index) => ({ job: query.data, site: sites[index] }))
      .filter((entry): entry is { job: ActiveExportJob; site: SiteSummary } => Boolean(entry.job && entry.site))
      .sort((a, b) => a.site.domain.localeCompare(b.site.domain));
  }, [jobQueries, sites]);

  const isLoading = sitesQuery.isLoading || jobQueries.some((query) => query.isLoading);
  const error = sitesQuery.error || jobQueries.find((query) => query.error)?.error || null;

  return {
    sites,
    activeJobs,
    isLoading,
    error,
    refetch: sitesQuery.refetch
  };
}
