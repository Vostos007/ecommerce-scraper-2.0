import { jwtVerify } from 'jose';
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

const PUBLIC_PATHS = new Set([
  '/',
  '/login',
  '/api/auth/login',
  '/api/auth/logout'
]);

const PUBLIC_PREFIXES = ['/_next', '/favicon', '/assets', '/public', '/api'];

const AUTH_COOKIE = 'auth-token';
const HEADER_NONCE = 'x-csp-nonce';
const AUTH_DISABLED = process.env.DASHBOARD_AUTH_DISABLED === 'true';

const BYPASS_USER_ID = process.env.DASHBOARD_AUTH_BYPASS_ID ?? 'admin-001';
const BYPASS_USERNAME = process.env.DASHBOARD_AUTH_BYPASS_USERNAME ?? 'admin';
const BYPASS_ROLE = (process.env.DASHBOARD_AUTH_BYPASS_ROLE as string | undefined) ?? 'admin';

interface TokenPayload {
  userId: string;
  username: string;
  role: string;
}

function isPublicPath(pathname: string): boolean {
  if (PUBLIC_PATHS.has(pathname)) {
    return true;
  }
  return PUBLIC_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

function buildNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
}

function buildCsp(nonce: string): string {
  const isProd = process.env.NODE_ENV === 'production';

  const scriptSrc = ["'self'", `'nonce-${nonce}'`];
  const connectSrc = ["'self'"];

  if (!isProd) {
    // Next.js dev сервер в dev-режиме использует eval + WebSocket для react-refresh.
    scriptSrc.push("'unsafe-eval'");
    connectSrc.push('ws://localhost:*', 'ws://127.0.0.1:*');
  }

  const styleDirective = isProd
    ? `style-src 'self' 'nonce-${nonce}' 'unsafe-inline'`
    : "style-src 'self' 'unsafe-inline'";

  const policies = [
    "default-src 'self'",
    "img-src 'self' data: blob:",
    `connect-src ${connectSrc.join(' ')}`,
    `script-src ${scriptSrc.join(' ')}`,
    styleDirective,
    "font-src 'self' data:",
    "frame-ancestors 'self'",
    "object-src 'none'",
    "base-uri 'self'"
  ];

  return policies.join('; ');
}

async function verifyToken(token: string): Promise<TokenPayload> {
  const secretValue = process.env.JWT_SECRET;
  if (!secretValue || secretValue.length < 32) {
    throw new Error('JWT_SECRET not configured');
  }
  const secret = new TextEncoder().encode(secretValue);
  const issuer = process.env.JWT_ISSUER ?? 'scraper-dashboard';
  const audience = process.env.JWT_AUDIENCE ?? 'dashboard-users';
  const { payload } = await jwtVerify(token, secret, { issuer, audience });

  const { userId, username, role } = payload as TokenPayload;
  if (!userId || !username || !role) {
    throw new Error('Invalid token payload');
  }
  return { userId, username, role };
}

function applySecurityHeaders(response: NextResponse, nonce: string): NextResponse {
  response.headers.set('Content-Security-Policy', buildCsp(nonce));
  response.headers.set('Strict-Transport-Security', 'max-age=63072000; includeSubDomains; preload');
  response.headers.set('X-Content-Type-Options', 'nosniff');
  response.headers.set('X-Frame-Options', 'SAMEORIGIN');
  response.headers.set('Referrer-Policy', 'origin-when-cross-origin');
  response.headers.set(
    'Permissions-Policy',
    'camera=(), microphone=(), geolocation=(), payment=(), usb=()'
  );
  response.headers.set('X-XSS-Protection', '0');
  response.headers.set(HEADER_NONCE, nonce);
  return response;
}

function unauthorizedResponse(request: NextRequest, cause: 'missing' | 'invalid'): NextResponse {
  if (request.nextUrl.pathname.startsWith('/api')) {
    return NextResponse.json({ error: 'Unauthorized', cause }, { status: 401 });
  }

  const url = request.nextUrl.clone();
  url.pathname = '/login';
  url.searchParams.set('next', request.nextUrl.pathname);
  return NextResponse.redirect(url);
}

async function logAuthFailure(request: NextRequest, reason: string) {
  console.warn('Неуспешная попытка аутентификации', {
    path: request.nextUrl.pathname,
    reason,
    ip: request.headers.get('x-forwarded-for') ?? 'unknown'
  });
}

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const nonce = buildNonce();

  if (AUTH_DISABLED) {
    const headers = new Headers(request.headers);
    headers.set(HEADER_NONCE, nonce);
    headers.set('x-user-id', BYPASS_USER_ID);
    headers.set('x-user-name', BYPASS_USERNAME);
    headers.set('x-user-role', BYPASS_ROLE);

    const response = NextResponse.next({ request: { headers } });
    response.headers.set('X-User-ID', BYPASS_USER_ID);
    response.headers.set('X-User-Role', BYPASS_ROLE);
    return applySecurityHeaders(response, nonce);
  }

  if (request.method === 'OPTIONS') {
    const response = new NextResponse(null, { status: 204 });
    response.headers.set('Access-Control-Allow-Origin', process.env.NEXT_PUBLIC_APP_URL ?? '*');
    response.headers.set('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
    response.headers.set('Access-Control-Allow-Headers', 'Content-Type, Authorization');
    response.headers.set('Access-Control-Allow-Credentials', 'true');
    return applySecurityHeaders(response, nonce);
  }

  if (isPublicPath(pathname)) {
    const headers = new Headers(request.headers);
    headers.set(HEADER_NONCE, nonce);
    const response = NextResponse.next({ request: { headers } });
    return applySecurityHeaders(response, nonce);
  }

  const token = request.cookies.get(AUTH_COOKIE)?.value;

  if (!token) {
    await logAuthFailure(request, 'missing_token');
    const response = unauthorizedResponse(request, 'missing');
    return applySecurityHeaders(response, nonce);
  }

  try {
    const payload = await verifyToken(token);
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set('x-user-id', payload.userId);
    requestHeaders.set('x-user-name', payload.username);
    requestHeaders.set('x-user-role', payload.role);
    requestHeaders.set(HEADER_NONCE, nonce);

    const response = NextResponse.next({ request: { headers: requestHeaders } });
    response.headers.set('X-User-ID', payload.userId);
    response.headers.set('X-User-Role', payload.role);
    return applySecurityHeaders(response, nonce);
  } catch (error) {
    await logAuthFailure(request, (error as Error).message ?? 'invalid_token');
    const response = unauthorizedResponse(request, 'invalid');
    response.cookies.set({
      name: AUTH_COOKIE,
      value: '',
      path: '/',
      maxAge: 0
    });
    return applySecurityHeaders(response, nonce);
  }
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|assets|public).*)']
};
