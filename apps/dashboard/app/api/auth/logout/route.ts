import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

import { logLogout } from '@/lib/audit';
import { createExpiredAuthCookie } from '@/lib/auth';
import { getUserFromRequest } from '@/lib/permissions';
import { decrementActiveSessions, withApiMetrics } from '@/lib/metrics';

function getIp(request: NextRequest): string {
  const forwarded = request.headers.get('x-forwarded-for');
  if (forwarded) {
    const candidate = forwarded.split(',')[0]?.trim();
    if (candidate) {
      return candidate;
    }
  }
  return (
    request.headers.get('x-real-ip') ??
    request.headers.get('true-client-ip') ??
    request.headers.get('cf-connecting-ip') ??
    'unknown'
  );
}

const handler = async (request: NextRequest) => {
  const auth = getUserFromRequest(request);
  const ip = getIp(request);
  const userAgent = request.headers.get('user-agent') ?? 'unknown';

  if (auth) {
    decrementActiveSessions();
    await logLogout({
      userId: auth.id,
      username: auth.username,
      role: auth.role,
      ip,
      userAgent
    });
  } else {
    await logLogout({
      userId: 'unknown',
      username: 'anonymous',
      ip,
      userAgent
    });
  }

  const response = NextResponse.json({ success: true, message: 'Logged out successfully' });
  response.headers.set('Set-Cookie', createExpiredAuthCookie());
  response.headers.set('Cache-Control', 'no-store');
  return response;
};

export const POST = withApiMetrics('auth_logout', handler);
