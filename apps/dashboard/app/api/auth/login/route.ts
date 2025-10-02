import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

import { logLogin } from '@/lib/audit';
import {
  createAuthCookie,
  generateJWT,
  getUserByUsername,
  updateLastLogin,
  validatePassword
} from '@/lib/auth';
import { incrementActiveSessions, recordLoginAttempt, withApiMetrics } from '@/lib/metrics';
import { TokenBucketLimiter } from '@/lib/rate-limit';
import { loginRequestSchema } from '@/lib/validations';

const attempts = Number(process.env.MAX_LOGIN_ATTEMPTS ?? 5);
const windowSeconds = Number(process.env.LOGIN_ATTEMPT_WINDOW ?? 60);

const limiter = new TokenBucketLimiter({
  capacity: attempts,
  windowMs: windowSeconds * 1000
});

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

async function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function ensureMinimumDuration(startedAt: number, minMs = 300) {
  const elapsed = Date.now() - startedAt;
  if (elapsed < minMs) {
    await sleep(minMs - elapsed);
  }
}

async function postHandler(request: NextRequest) {
  const startedAt = Date.now();
  const ip = getIp(request);
  const userAgent = request.headers.get('user-agent') ?? 'unknown';

  if (!limiter.take(ip)) {
    recordLoginAttempt('rate_limited');
    await logLogin({
      userId: 'unknown',
      username: 'unknown',
      success: false,
      ip,
      userAgent,
      error: 'rate_limited'
    });
    await ensureMinimumDuration(startedAt);
    return NextResponse.json({ error: 'Превышено количество попыток входа. Повторите позже.' }, { status: 429 });
  }

  let payload: { username: string; password: string };
  try {
    const json = await request.json();
    payload = loginRequestSchema.parse(json);
  } catch {
    recordLoginAttempt('failure');
    await ensureMinimumDuration(startedAt);
    return NextResponse.json({ error: 'Некорректный формат запроса' }, { status: 400 });
  }

  const user = await getUserByUsername(payload.username);

  const passwordMatches = user ? await validatePassword(user, payload.password) : false;
  const success = Boolean(user && passwordMatches);

  if (!success) {
    recordLoginAttempt('failure');
    await logLogin({
      userId: user?.id ?? 'unknown',
      username: payload.username,
      ...(user?.role ? { role: user.role } : {}),
      success: false,
      ip,
      userAgent,
      error: 'invalid_credentials'
    });
    await ensureMinimumDuration(startedAt);
    return NextResponse.json({ error: 'Неверное имя пользователя или пароль' }, { status: 401 });
  }

  const token = await generateJWT(user!);
  await updateLastLogin(user!.id);
  limiter.reset(ip);
  recordLoginAttempt('success');
  incrementActiveSessions();
  await logLogin({
    userId: user!.id,
    username: user!.username,
    ...(user!.role ? { role: user!.role } : {}),
    success: true,
    ip,
    userAgent
  });

  const response = NextResponse.json({
    id: user!.id,
    username: user!.username,
    role: user!.role
  });

  response.headers.set('Set-Cookie', createAuthCookie(token, user!.role));
  await ensureMinimumDuration(startedAt);
  return response;
}

export const POST = withApiMetrics('auth_login', postHandler);
