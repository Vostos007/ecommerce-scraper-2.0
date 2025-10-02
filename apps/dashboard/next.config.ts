import type { NextConfig } from 'next';

const appOrigin = process.env.NEXT_PUBLIC_APP_URL ?? 'http://localhost:3050';

const securityHeaders = [
  {
    key: 'Strict-Transport-Security',
    value: 'max-age=63072000; includeSubDomains; preload'
  },
  {
    key: 'X-Content-Type-Options',
    value: 'nosniff'
  },
  {
    key: 'X-Frame-Options',
    value: 'SAMEORIGIN'
  },
  {
    key: 'Referrer-Policy',
    value: 'origin-when-cross-origin'
  },
  {
    key: 'Permissions-Policy',
    value: 'camera=(), microphone=(), geolocation=(), payment=(), usb=()'
  }
];

const corsHeaders = [
  {
    key: 'Access-Control-Allow-Origin',
    value: appOrigin
  },
  {
    key: 'Access-Control-Allow-Credentials',
    value: 'true'
  },
  {
    key: 'Access-Control-Allow-Headers',
    value: 'Content-Type, Authorization, X-Requested-With'
  },
  {
    key: 'Access-Control-Allow-Methods',
    value: 'GET,POST,PUT,DELETE,OPTIONS'
  }
];

const config: NextConfig = {
  reactStrictMode: true,
  turbopack: {
    // В dev режиме оставляем стандартные настройки; возможность точечной настройки сохранена на будущее.
  },
  async headers() {
    return [
      {
        source: '/:path*',
        headers: securityHeaders
      },
      {
        source: '/api/:path*',
        headers: corsHeaders
      }
    ];
  }
};

export default config;
