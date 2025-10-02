import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

import { logAuditEvent } from './audit';

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

const ROLE_HIERARCHY: Role[] = ['viewer', 'operator', 'admin'];

const ROLE_PERMISSIONS: Record<Role, ReadonlySet<Permission>> = {
  admin: new Set<Permission>([
    'export:start',
    'export:stop',
    'export:view',
    'files:upload',
    'files:download',
    'files:delete',
    'logs:view',
    'stats:view',
    'proxy:manage',
    'users:create',
    'users:edit',
    'users:delete',
    'config:edit'
  ]),
  operator: new Set<Permission>([
    'export:start',
    'export:stop',
    'export:view',
    'files:upload',
    'files:download',
    'logs:view',
    'stats:view',
    'proxy:manage'
  ]),
  viewer: new Set<Permission>(['export:view', 'files:download', 'logs:view', 'stats:view'])
};

export interface AuthenticatedUser {
  id: string;
  username: string;
  role: Role;
}

export interface AuthHeaders {
  'x-user-id': string;
  'x-user-name': string;
  'x-user-role': Role;
}

export const ROLE_PERMISSIONS_LIST: Record<Role, Permission[]> = {
  admin: [...ROLE_PERMISSIONS.admin],
  operator: [...ROLE_PERMISSIONS.operator],
  viewer: [...ROLE_PERMISSIONS.viewer]
};

export function getPermissions(role: Role): Permission[] {
  return ROLE_PERMISSIONS_LIST[role] ?? [];
}

export function hasPermission(role: Role, permission: Permission): boolean {
  const permissions = ROLE_PERMISSIONS[role];
  return permissions ? permissions.has(permission) : false;
}

export function requirePermission(role: Role, permission: Permission): void {
  if (!hasPermission(role, permission)) {
    throw new Error('Недостаточно прав для выполнения операции');
  }
}

export function isRoleAtLeast(role: Role, target: Role): boolean {
  return ROLE_HIERARCHY.indexOf(role) >= ROLE_HIERARCHY.indexOf(target);
}

export function getUserFromRequest(request: NextRequest): AuthenticatedUser | null {
  const id = request.headers.get('x-user-id');
  const username = request.headers.get('x-user-name');
  const role = request.headers.get('x-user-role') as Role | null;

  if (!id || !username || !role) {
    return null;
  }

  return { id, username, role };
}

type RouteHandlerContext = Record<string, unknown>;

type NextRouteHandler<TContext extends RouteHandlerContext = RouteHandlerContext> = (
  request: NextRequest,
  context: TContext
) => Promise<Response> | Response;

type AuthenticatedRouteHandler<TContext extends RouteHandlerContext = RouteHandlerContext> = (
  request: NextRequest,
  context: TContext,
  auth: AuthenticatedUser
) => Promise<Response> | Response;

export function withAuth<TContext extends RouteHandlerContext = RouteHandlerContext>(
  handler: AuthenticatedRouteHandler<TContext>,
  requiredPermission?: Permission
): NextRouteHandler<TContext> {
  return async (request, context) => {
    const auth = getUserFromRequest(request);

    if (!auth) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
    }

    if (requiredPermission && !hasPermission(auth.role, requiredPermission)) {
      void logAuditEvent({
        action: 'access_denied',
        timestamp: new Date().toISOString(),
        userId: auth.id,
        username: auth.username,
        role: auth.role,
        success: false,
        ip: request.headers.get('x-forwarded-for') ?? 'unknown',
        userAgent: request.headers.get('user-agent') ?? 'unknown',
        details: { requiredPermission }
      });

      return NextResponse.json({ error: 'Forbidden' }, { status: 403 });
    }

    return handler(request, context, auth);
  };
}

export function canAccess(role: Role, permission: Permission | Permission[]): boolean {
  if (Array.isArray(permission)) {
    return permission.every((perm) => hasPermission(role, perm));
  }
  return hasPermission(role, permission);
}

