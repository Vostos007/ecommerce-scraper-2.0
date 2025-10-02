'use client';

import { useMemo, useCallback } from 'react';

import { useAuthContext } from '@/components/providers/AuthProvider';

export type Role = 'admin' | 'operator' | 'viewer';

export type Permission =
  | 'export:start'
  | 'export:stop'
  | 'export:view'
  | 'files:upload'
  | 'files:download'
  | 'files:delete'
  | 'logs:view'
  | 'stats:view'
  | 'proxy:manage'
  | 'users:create'
  | 'users:edit'
  | 'users:delete'
  | 'config:edit';

const ROLE_ORDER: Record<Role, number> = {
  viewer: 0,
  operator: 1,
  admin: 2
};

export function useAuth() {
  const { user, isLoading, isAuthenticated, error, login, logout, refreshUser, clearError } = useAuthContext();

  const permissionSet = useMemo(() => new Set(user?.permissions ?? []), [user?.permissions]);

  const hasPermission = useCallback(
    (permission: Permission) => {
      return permissionSet.has(permission);
    },
    [permissionSet]
  );

  const hasPermissions = useCallback(
    (permissions: Permission[], strategy: 'all' | 'any' = 'all') => {
      if (permissions.length === 0) {
        return true;
      }
      if (strategy === 'any') {
        return permissions.some((permission) => permissionSet.has(permission));
      }
      return permissions.every((permission) => permissionSet.has(permission));
    },
    [permissionSet]
  );

  const requireRole = useCallback(
    (role: Role) => {
      if (!user) {
        return false;
      }
      return ROLE_ORDER[user.role] >= ROLE_ORDER[role];
    },
    [user]
  );

  const isAdmin = useCallback(() => user?.role === 'admin', [user?.role]);
  const isOperator = useCallback(() => ROLE_ORDER[user?.role ?? 'viewer'] >= ROLE_ORDER.operator, [user?.role]);

  const getUserRole = useCallback(() => user?.role ?? null, [user?.role]);
  const getUserId = useCallback(() => user?.id ?? null, [user?.id]);

  const canAccess = useCallback(
    (permission: Permission | Permission[], strategy: 'all' | 'any' = 'all') => {
      return Array.isArray(permission)
        ? hasPermissions(permission, strategy)
        : hasPermission(permission);
    },
    [hasPermission, hasPermissions]
  );

  const canAccessResource = useCallback(
    (_resource: string, permission: Permission | Permission[], strategy: 'all' | 'any' = 'all') => {
      return canAccess(permission, strategy);
    },
    [canAccess]
  );

  return {
    user,
    isLoading,
    isAuthenticated,
    error,
    login,
    logout,
    refreshUser,
    clearError,
    hasPermission,
    hasPermissions,
    canAccess,
    canAccessResource,
    requireRole,
    isAdmin,
    isOperator,
    getUserRole,
    getUserId
  };
}

