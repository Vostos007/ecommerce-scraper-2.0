'use client';

import { DownloadCenter } from '@/components/DownloadCenter';

export default function DownloadsPage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="space-y-2">
        <h1 className="text-3xl font-bold text-foreground">Загрузки</h1>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Управляйте сводными отчётами и скачивайте последние выгрузки для каждой площадки.
        </p>
      </div>
      <DownloadCenter />
    </div>
  );
}
