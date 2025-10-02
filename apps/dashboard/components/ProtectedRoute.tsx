'use client';

import { type ReactNode } from 'react';

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

export function ProtectedRoute({ children }: ProtectedRouteProps) {
  // Авторизация отключена: всегда показываем контент.
  useAuth();
  return <>{children}</>;
}

interface PermissionGateProps {
  permission: Permission | Permission[];
  strategy?: 'all' | 'any';
  fallback?: ReactNode;
  children: ReactNode;
}

export function PermissionGate({ permission, strategy = 'all', fallback = null, children }: PermissionGateProps) {
  useAuth();
  return <>{children}</>;
}

interface RoleGateProps {
  role: Role;
  fallback?: ReactNode;
  children: ReactNode;
}

export function RoleGate({ role, fallback = null, children }: RoleGateProps) {
  useAuth();
  return <>{children}</>;
}
