'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useMemo, useRef, useState } from 'react';

import type { UploadResult } from '@/lib/api';

const MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024;

export interface UploadStatus {
  isUploading: boolean;
  error: string | null;
  success: boolean;
  savedPath: string | null;
  progress: number | null;
}

function validateFile(file: File): string | null {
  const mime = file.type || 'application/json';
  if (!['application/json', 'text/json', 'application/octet-stream'].includes(mime)) {
    return 'Допускаются только JSON файлы';
  }
  if (file.size > MAX_FILE_SIZE_BYTES) {
    return 'Размер файла превышает 50MB';
  }
  return null;
}

export function useFileUpload(site: string) {
  const queryClient = useQueryClient();
  const [progress, setProgress] = useState<number | null>(null);
  const requestRef = useRef<XMLHttpRequest | null>(null);

  const mutation = useMutation<UploadResult, Error, File>({
    mutationFn: async (file) => {
      const validationError = validateFile(file);
      if (validationError) {
        throw new Error(validationError);
      }

      let parsed: unknown;
      try {
        parsed = JSON.parse(await file.text());
      } catch {
        throw new Error('Файл содержит некорректный JSON');
      }

      if (!parsed || typeof parsed !== 'object' || !Array.isArray((parsed as { links?: unknown }).links)) {
        throw new Error('JSON карта должна содержать поле links с массивом ссылок');
      }

      const formData = new FormData();
      formData.append('site', site);
      formData.append('file', file);

      return await new Promise<UploadResult>((resolve, reject) => {
        const request = new XMLHttpRequest();
        request.open('POST', '/api/upload');
        requestRef.current = request;

        request.upload.onprogress = (event) => {
          if (event.lengthComputable) {
            const percentage = Math.round((event.loaded / event.total) * 100);
            setProgress(percentage);
          }
        };

        request.onload = () => {
          requestRef.current = null;
          try {
            const payload = JSON.parse(request.responseText || '{}');
            if (request.status >= 200 && request.status < 300) {
              resolve(payload as UploadResult);
            } else {
              reject(new Error(payload.error ?? 'Не удалось загрузить файл'));
            }
          } catch (parseError) {
            reject(parseError instanceof Error ? parseError : new Error('Не удалось обработать ответ сервера'));
          }
        };

        request.onerror = () => {
          requestRef.current = null;
          reject(new Error('Ошибка сети, попробуйте позже'));
        };

        request.onabort = () => {
          requestRef.current = null;
          reject(new Error('Загрузка отменена'));
        };

        request.send(formData);
      });
    },
    onSuccess: () => {
      setProgress(null);
      void queryClient.invalidateQueries({ queryKey: ['sites'] });
      void queryClient.invalidateQueries({ queryKey: ['site', site] });
    },
    onError: () => {
      setProgress(null);
    }
  });

  const upload = async (file: File) => {
    setProgress(0);
    await mutation.mutateAsync(file);
  };

  const cancel = () => {
    if (requestRef.current) {
      requestRef.current.abort();
      requestRef.current = null;
    } else {
      mutation.reset();
    }
    setProgress(null);
  };

  const status = useMemo<UploadStatus>(() => ({
    isUploading: mutation.isPending,
    error: mutation.error?.message ?? null,
    success: mutation.isSuccess,
    savedPath: mutation.data?.savedPath ?? null,
    progress
  }), [mutation.data, mutation.error, mutation.isPending, mutation.isSuccess, progress]);

  return { upload, cancel, status, reset: mutation.reset };
}
