'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  addDatacenterProxies,
  deleteDatacenterProxies,
  getDatacenterProxies,
  type ProxyDeleteResponse,
  type ProxyList,
  type ProxyMutationResponse
} from '@/lib/api';

interface AddOptions {
  proxies: string[];
  replace?: boolean;
}

type AddResult = ProxyMutationResponse;
type DeleteResult = ProxyDeleteResponse;

const QUERY_KEY = ['proxy-pool'];

export function useProxyPool() {
  const queryClient = useQueryClient();

  const query = useQuery<ProxyList, Error>({
    queryKey: QUERY_KEY,
    queryFn: () => getDatacenterProxies(),
    staleTime: 30_000,
    refetchOnWindowFocus: false
  });

  const addMutation = useMutation<AddResult, Error, AddOptions>({
    mutationFn: (input) =>
      addDatacenterProxies({
        proxies: input.proxies,
        ...(input.replace !== undefined ? { replace: input.replace } : {})
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    }
  });

  const deleteMutation = useMutation<DeleteResult, Error, string[]>({
    mutationFn: (proxies) => deleteDatacenterProxies(proxies),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    }
  });

  return {
    data: query.data,
    isLoading: query.isLoading,
    isFetching: query.isFetching,
    error: query.error,
    refetch: query.refetch,
    addProxies: addMutation.mutateAsync,
    isAdding: addMutation.isPending,
    removeProxies: deleteMutation.mutateAsync,
    isRemoving: deleteMutation.isPending,
    addError: addMutation.error,
    removeError: deleteMutation.error
  };
}
