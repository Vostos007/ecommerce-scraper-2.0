import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

const HEADER_NONCE = 'x-csp-nonce';

function buildNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
}

function buildCsp(nonce: string): string {
  const isProd = process.env.NODE_ENV === 'production';
  const scriptSrc = ["'self'", `'nonce-${nonce}'`];
  const connectSrc = ["'self'`];

  if (!isProd) {
    scriptSrc.push("'unsafe-eval'");
    connectSrc.push('ws://localhost:*', 'ws://127.0.0.1:*');
  }

  const styleDirective = isProd
    ? `style-src 'self' 'nonce-${nonce}' 'unsafe-inline'`
    : "style-src 'self' 'unsafe-inline'";

  return [
    "default-src 'self'",
    "img-src 'self' data: blob:",
    `connect-src ${connectSrc.join(' ')}`,
    `script-src ${scriptSrc.join(' ')}`,
    styleDirective,
    "font-src 'self' data:",
    "frame-ancestors 'self'",
    "object-src 'none'",
    "base-uri 'self'"
  ].join('; ');
}

function applySecurity(response: NextResponse, nonce: string): NextResponse {
  response.headers.set('Content-Security-Policy', buildCsp(nonce));
  response.headers.set('Strict-Transport-Security', 'max-age=63072000; includeSubDomains; preload');
  response.headers.set('X-Content-Type-Options', 'nosniff');
  response.headers.set('X-Frame-Options', 'SAMEORIGIN');
  response.headers.set('Referrer-Policy', 'origin-when-cross-origin');
  response.headers.set('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=(), usb=()');
  response.headers.set('X-XSS-Protection', '0');
  response.headers.set(HEADER_NONCE, nonce);
  return response;
}

export function middleware(request: NextRequest) {
  const nonce = buildNonce();
  const headers = new Headers(request.headers);
  headers.set(HEADER_NONCE, nonce);

  const response = NextResponse.next({ request: { headers } });
  return applySecurity(response, nonce);
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|assets|public).*)']
};
