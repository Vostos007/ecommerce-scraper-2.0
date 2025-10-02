'use client';

import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react';

import type { AuthUser, LoginCredentials } from '@/lib/api';
import { permissionSchema } from '@/lib/validations';

interface AuthContextValue {
  user: AuthUser;
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
  const defaultUser: AuthUser = useMemo(
    () => ({
      id: 'demo-user',
      username: 'demo',
      role: 'operator',
      createdAt: new Date('2025-01-01T00:00:00Z').toISOString(),
      lastLogin: new Date('2025-01-01T00:00:00Z').toISOString(),
      active: true,
      permissions: permissionSchema.options
    }),
    []
  );

  const login = useCallback(async (_credentials: LoginCredentials) => {
    // Авторизация отключена — считаем, что пользователь уже вошёл.
    return Promise.resolve();
  }, []);

  const logout = useCallback(async () => Promise.resolve(), []);
  const refreshUser = useCallback(async () => Promise.resolve(), []);
  const clearError = useCallback(() => undefined, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user: defaultUser,
      isLoading: false,
      isAuthenticated: true,
      error: null,
      login,
      logout,
      refreshUser,
      clearError
    }),
    [defaultUser, login, logout, refreshUser, clearError]
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
