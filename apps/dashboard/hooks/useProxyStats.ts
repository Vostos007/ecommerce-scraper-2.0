'use client';

import { useQuery } from '@tanstack/react-query';

import { getProxyStats, type ProxyStats } from '@/lib/api';

export function useProxyStats() {
  const query = useQuery<ProxyStats, Error>({
    queryKey: ['proxy-stats'],
    queryFn: getProxyStats,
    staleTime: 30_000,
    refetchInterval: 30_000,
    refetchOnWindowFocus: false,
    retry: 2
  });

  return {
    data: query.data ?? null,
    error: query.error?.message ?? null,
    isLoading: query.isLoading,
    refetch: query.refetch
  };
}
