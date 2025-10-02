import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

import { getUserById } from '@/lib/auth';
import { getPermissions, getUserFromRequest } from '@/lib/permissions';
import { withApiMetrics } from '@/lib/metrics';

const handler = async (request: NextRequest) => {
  const auth = getUserFromRequest(request);
  if (!auth) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const user = await getUserById(auth.id);
  if (!user) {
    return NextResponse.json({ error: 'Пользователь не найден' }, { status: 404 });
  }

  const permissions = getPermissions(user.role);
  const response = NextResponse.json({
    id: user.id,
    username: user.username,
    role: user.role,
    lastLogin: user.lastLogin,
    permissions
  });
  response.headers.set('Cache-Control', 'no-store, no-cache, must-revalidate');
  return response;
};

export const GET = withApiMetrics('auth_me', handler);
