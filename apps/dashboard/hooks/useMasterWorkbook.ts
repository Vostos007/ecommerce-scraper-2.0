'use client';

import { useCallback, useState } from 'react';

import { useMutation, useQuery } from '@tanstack/react-query';

import {
  downloadMasterWorkbook,
  getMasterWorkbookStatus,
  type DownloadProgress,
  type MasterWorkbookStatus
} from '@/lib/api';

interface DownloadResult {
  filename: string;
  size?: number;
}

export function useMasterWorkbook() {
  const [progress, setProgress] = useState<DownloadProgress | null>(null);

  const statusQuery = useQuery<MasterWorkbookStatus, Error>({
    queryKey: ['master-workbook-status'],
    queryFn: getMasterWorkbookStatus,
    refetchInterval: (query) => (query.state.data?.status === 'generating' ? 5_000 : false),
    refetchOnWindowFocus: false,
    staleTime: 30_000
  });

  const downloadMutation = useMutation<DownloadResult, Error>({
    mutationFn: async () => {
      setProgress({ loaded: 0 });
      const blob = await downloadMasterWorkbook((step) => {
        setProgress(step);
      });
      const filename = `master-workbook-${new Date().toISOString().replace(/[:.]/g, '-')}.xlsx`;
      const url = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = filename;
        anchor.style.display = 'none';
        document.body.append(anchor);
        anchor.click();
        anchor.remove();
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1_000);
      }
      return { filename, size: blob.size };
    },
    onSettled: () => {
      setProgress(null);
      statusQuery.refetch();
    }
  });

  const triggerDownload = useCallback(() => {
    if (!downloadMutation.isPending) {
      downloadMutation.reset();
      downloadMutation.mutate();
    }
  }, [downloadMutation]);

  const status = statusQuery.data;
  const phase = status?.phase ?? null;
  const stale = status?.stale ?? false;

  return {
    status,
    isGenerating: status?.status === 'generating',
    isReady: status?.status === 'ready',
    isDownloading: downloadMutation.isPending,
    error: downloadMutation.error?.message ?? statusQuery.error?.message ?? null,
    triggerDownload,
    fileInfo: status?.status === 'ready'
      ? {
          size: status.file_size ?? null,
          generatedAt: status.generated_at ?? null
        }
      : null,
    phase,
    isStale: stale,
    progress
  };
}
