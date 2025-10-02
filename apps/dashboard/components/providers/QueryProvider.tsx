'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { type ReactNode, useState } from 'react';

interface QueryProviderProps {
  children: ReactNode;
}

function createClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5 * 60 * 1000,
        gcTime: 10 * 60 * 1000,
        retry: 3,
        refetchOnWindowFocus: false
      },
      mutations: {
        retry: 1
      }
    }
  });
}

export function QueryProvider({ children }: QueryProviderProps) {
  const [client] = useState(() => createClient());
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
