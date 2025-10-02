'use client';

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

import { getCurrentUser, loginRequest, logoutRequest, type AuthUser, type LoginCredentials } from '@/lib/api';

interface AuthContextValue {
  user: AuthUser | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  error: string | null;
  login: (credentials: LoginCredentials) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
  clearError: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const {
    data: user,
    isLoading,
    refetch,
    isFetching
  } = useQuery<AuthUser | null>({
    queryKey: ['auth', 'me'],
    queryFn: getCurrentUser,
    retry: false,
    staleTime: 0
  });

  const login = useCallback(
    async (credentials: LoginCredentials) => {
      setError(null);
      try {
        await loginRequest(credentials);
        const refreshed = await queryClient.fetchQuery({
          queryKey: ['auth', 'me'],
          queryFn: getCurrentUser,
          staleTime: 0
        });
        queryClient.setQueryData(['auth', 'me'], refreshed);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Не удалось выполнить вход';
        setError(message);
        throw err;
      }
    },
    [queryClient]
  );

  const logout = useCallback(async () => {
    try {
      await logoutRequest();
    } finally {
      queryClient.setQueryData(['auth', 'me'], null);
    }
  }, [queryClient]);

  const refreshUser = useCallback(async () => {
    setError(null);
    await refetch({ throwOnError: false });
  }, [refetch]);

  const clearError = useCallback(() => setError(null), []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user: user ?? null,
      isLoading: isLoading || isFetching,
      isAuthenticated: Boolean(user),
      error,
      login,
      logout,
      refreshUser,
      clearError
    }),
    [user, isLoading, isFetching, error, login, logout, refreshUser, clearError]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuthContext(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuthContext должен использоваться внутри AuthProvider');
  }
  return context;
}
