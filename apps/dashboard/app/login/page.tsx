import type { Metadata } from 'next';
import { Suspense } from 'react';

import LoginClient from './LoginClient';

export const metadata: Metadata = {
  title: 'Вход в Scraper Dashboard',
  description: 'Авторизация для доступа к внутреннему панели управления скрейпингом.',
  robots: {
    index: false,
    follow: false
  }
};

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">Загрузка...</div>}>
      <LoginClient />
    </Suspense>
  );
}

