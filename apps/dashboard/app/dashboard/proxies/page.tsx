'use client';

import { ProxyStats } from '@/components/ProxyStats';

export default function ProxiesPage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="space-y-2">
        <h1 className="text-3xl font-bold text-foreground">Прокси инфраструктура</h1>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Отслеживайте состояние пула прокси, успешность запросов и рекомендации по масштабированию.
        </p>
      </div>
      <ProxyStats />
    </div>
  );
}
