import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import { headers } from 'next/headers';

import './globals.css';
import { TopNav } from '@/components/TopNav';
import { AuthProvider } from '@/components/providers/AuthProvider';
import { QueryProvider } from '@/components/providers/QueryProvider';

export const metadata: Metadata = {
  title: 'UI Dashboard - Scraper Management',
  description:
    'Внутренний UI Dashboard для управления Python экспортерами, мониторинга логов и состояния прокси.'
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  maximumScale: 1
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  const headerList = await headers();
  const nonce = headerList.get('x-csp-nonce') ?? undefined;

  return (
    <html lang="ru" suppressHydrationWarning>
      <body
        suppressHydrationWarning
        className="min-h-screen bg-background font-sans text-foreground antialiased"
        {...(nonce ? { 'data-csp-nonce': nonce } : {})}
      >
        <QueryProvider>
          <AuthProvider>
            <TopNav />
            <main className="mx-auto w-full max-w-6xl px-4 pb-10 pt-6 sm:px-6">
              {children}
            </main>
          </AuthProvider>
        </QueryProvider>
      </body>
    </html>
  );
}
