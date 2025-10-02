'use client';

import { ChangeEvent, useEffect, useState } from 'react';

import { Button } from './ui/button';
import { useFileUpload, type UploadStatus } from '../hooks/useFileUpload';
import { cn, formatBytes } from '../lib/utils';

interface FileUploadProps {
  site: string;
  onStatusChange?: (status: UploadStatus) => void;
}

export function FileUpload({ site, onStatusChange }: FileUploadProps) {
  const { upload, cancel, status, reset } = useFileUpload(site);
  const [fileName, setFileName] = useState<string | null>(null);
  const [fileSize, setFileSize] = useState<number>(0);

  useEffect(() => {
    onStatusChange?.(status);
  }, [onStatusChange, status]);

  async function handleChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    setFileName(file.name);
    setFileSize(file.size);
    try {
      await upload(file);
    } catch {
      // Ошибка отображается через статус; подавляем всплытие исключения.
    }
  }

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-6 space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-slate-100">Загрузка JSON карты</h2>
        <p className="text-sm text-slate-400">
          Файлы валидируются на стороне сервера и сохраняются в <code>data/sites/{site}/</code>.
        </p>
      </div>

      <label
        className={cn(
          'flex h-32 cursor-pointer flex-col items-center justify-center rounded-lg border border-dashed border-slate-700 text-sm text-slate-400 transition-colors hover:border-slate-500',
          status.error && 'border-rose-500 text-rose-400',
          status.isUploading && 'border-primary/60'
        )}
      >
        <input type="file" accept="application/json" className="hidden" onChange={handleChange} />
        <span className="text-sm font-medium">Перетащите JSON или нажмите для выбора</span>
        {fileName && (
          <span className="mt-1 text-xs text-slate-500">
            {fileName} · {formatBytes(fileSize)}
          </span>
        )}
      </label>

      {status.progress !== null && (
        <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
          <div
            className="h-full bg-primary transition-all"
            style={{ width: `${Math.min(status.progress, 100)}%` }}
          />
        </div>
      )}

      {status.error && <div className="text-sm text-rose-400">Ошибка: {status.error}</div>}
      {status.success && (
        <div className="text-sm text-emerald-400">
          Файл сохранён: {status.savedPath}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {status.isUploading && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              cancel();
            }}
          >
            Отменить
          </Button>
        )}
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={status.isUploading}
          onClick={() => {
            setFileName(null);
            setFileSize(0);
            reset();
          }}
        >
          Сбросить
        </Button>
      </div>
    </div>
  );
}
