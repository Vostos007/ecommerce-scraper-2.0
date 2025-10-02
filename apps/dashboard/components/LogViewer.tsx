'use client';

import { useEffect, useRef } from 'react';

import { useSSELogs } from '../hooks/useSSELogs';
import { cn } from '../lib/utils';

interface LogViewerProps {
  jobId: string | null;
}

export function LogViewer({ jobId }: LogViewerProps) {
  const { logs } = useSSELogs(jobId);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  return (
    <div className="h-72 overflow-y-auto rounded-xl border border-slate-800 bg-slate-950/50 p-4 text-xs font-mono text-slate-200">
      {jobId == null ? (
        <p className="text-slate-500">Логи появятся после запуска экспорта.</p>
      ) : logs.length === 0 ? (
        <p className="text-slate-500">Ждём первые сообщения…</p>
      ) : (
        logs.map((entry, index) => (
          <div
            key={`${entry.t}-${entry.k}-${entry.idx}-${index}`}
            className={cn(entry.k === 'err' ? 'text-rose-400' : 'text-slate-200')}
          >
            <span className="text-slate-500">[{new Date(entry.t).toLocaleTimeString()}]</span>{' '}
            {entry.m}
          </div>
        ))
      )}
      <div ref={bottomRef} />
    </div>
  );
}
