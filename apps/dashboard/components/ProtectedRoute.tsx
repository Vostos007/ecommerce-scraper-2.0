'use client';

import { useEffect, useMemo, type ReactNode } from 'react';
import { usePathname, useRouter } from 'next/navigation';

import { useAuth, type Permission, type Role } from '@/hooks/useAuth';

interface ProtectedRouteProps {
  children: ReactNode;
  requiredPermission?: Permission | Permission[];
  requiredRole?: Role;
  fallback?: ReactNode;
  redirectTo?: string;
  permissionStrategy?: 'all' | 'any';
}

function DefaultFallback() {
  return (
    <div className="rounded-md border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
      Доступ ограничен. Обратитесь к администратору, чтобы получить необходимые права.
    </div>
  );
}

export function ProtectedRoute({
  children,
  requiredPermission,
  requiredRole,
  fallback,
  redirectTo = '/login',
  permissionStrategy = 'all'
}: ProtectedRouteProps) {
  const { isAuthenticated, isLoading, canAccess, requireRole, user } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      const target = `${redirectTo}?next=${encodeURIComponent(pathname ?? '/')}`;
      router.replace(target);
    }
  }, [isAuthenticated, isLoading, redirectTo, router, pathname]);

  const authorized = useMemo(() => {
    if (!isAuthenticated || !user) {
      return false;
    }
    if (requiredRole && !requireRole(requiredRole)) {
      return false;
    }
    if (requiredPermission && !canAccess(requiredPermission, permissionStrategy)) {
      return false;
    }
    return true;
  }, [isAuthenticated, user, requiredRole, requireRole, requiredPermission, canAccess, permissionStrategy]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-10 text-sm text-muted-foreground">
        Проверяем доступ...
      </div>
    );
  }

  if (!isAuthenticated) {
    return null;
  }

  if (!authorized) {
    return <>{fallback ?? <DefaultFallback />}</>;
  }

  return <>{children}</>;
}

interface PermissionGateProps {
  permission: Permission | Permission[];
  strategy?: 'all' | 'any';
  fallback?: ReactNode;
  children: ReactNode;
}

export function PermissionGate({ permission, strategy = 'all', fallback = null, children }: PermissionGateProps) {
  const { canAccess } = useAuth();
  if (!canAccess(permission, strategy)) {
    return <>{fallback}</>;
  }
  return <>{children}</>;
}

interface RoleGateProps {
  role: Role;
  fallback?: ReactNode;
  children: ReactNode;
}

export function RoleGate({ role, fallback = null, children }: RoleGateProps) {
  const { requireRole } = useAuth();
  if (!requireRole(role)) {
    return <>{fallback}</>;
  }
  return <>{children}</>;
}

