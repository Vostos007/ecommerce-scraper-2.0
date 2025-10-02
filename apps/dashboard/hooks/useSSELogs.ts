'use client';

import { useEffect, useRef, useState } from 'react';

type LogKind = 'out' | 'err' | 'meta';

export interface LogEntry {
  idx: number;
  t: number;
  k: LogKind;
  m: string;
}

export function useSSELogs(jobId: string | null) {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const counterRef = useRef(0);

  useEffect(() => {
    if (!jobId) {
      setLogs([]);
      return;
    }

    const events = new EventSource(`/api/streams/${jobId}`);

    function push(kind: LogKind, message: string, timestamp: number) {
      counterRef.current += 1;
      setLogs((prev) => [
        ...prev,
        {
          idx: counterRef.current,
          k: kind,
          m: message,
          t: timestamp
        }
      ].slice(-2000));
    }

    events.addEventListener('init', (_event) => {
      push('meta', `Запущен job ${jobId}`, Date.now());
    });

    events.addEventListener('stdout', (event) => {
      const payload = JSON.parse((event as MessageEvent<string>).data) as { message: string; ts: number };
      push('out', payload.message, payload.ts);
    });

    events.addEventListener('stderr', (event) => {
      const payload = JSON.parse((event as MessageEvent<string>).data) as { message: string; ts: number };
      push('err', payload.message, payload.ts);
    });

    events.addEventListener('end', (_event) => {
      const payload = JSON.parse((_event as MessageEvent<string>).data) as {
        code: number | null;
        signal: string | null;
        ts: number;
      };
      push('meta', `Завершено (code=${payload.code ?? 'null'}, signal=${payload.signal ?? 'none'})`, payload.ts);
      events.close();
    });

    events.onerror = () => {
      push('err', 'SSE соединение оборвалось', Date.now());
      events.close();
    };

    return () => {
      events.close();
    };
  }, [jobId]);

  return { logs };
}
