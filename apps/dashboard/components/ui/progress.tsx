import { forwardRef, type HTMLAttributes } from 'react';

import { cn } from '@/lib/utils';

const VARIANTS = {
  default: 'bg-slate-300 dark:bg-slate-700',
  success: 'bg-emerald-400 dark:bg-emerald-500',
  warning: 'bg-amber-400 dark:bg-amber-500',
  error: 'bg-rose-500 dark:bg-rose-500'
};

const TRACK_VARIANTS = {
  default: 'bg-slate-800/40',
  success: 'bg-emerald-900/40',
  warning: 'bg-amber-900/40',
  error: 'bg-rose-900/40'
};

const SIZES = {
  sm: 'h-1.5',
  md: 'h-2.5',
  lg: 'h-3.5'
};

export interface ProgressProps extends HTMLAttributes<HTMLDivElement> {
  value?: number;
  variant?: keyof typeof VARIANTS;
  size?: keyof typeof SIZES;
  indeterminate?: boolean;
}

export const Progress = forwardRef<HTMLDivElement, ProgressProps>(
  (
    {
      className,
      value,
      variant = 'default',
      size = 'md',
      indeterminate = false,
      ...props
    },
    ref
  ) => {
    const clampedValue = value !== undefined ? Math.max(0, Math.min(100, value)) : 0;

    return (
      <div
        ref={ref}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={indeterminate ? undefined : clampedValue}
        className={cn('w-full overflow-hidden rounded-full', TRACK_VARIANTS[variant], SIZES[size], className)}
        {...props}
      >
        <div
          className={cn(
            'h-full w-full origin-left transition-transform duration-500 ease-out',
            VARIANTS[variant],
            indeterminate && 'animate-pulse'
          )}
          style={{ transform: indeterminate ? 'scaleX(0.4)' : `scaleX(${clampedValue / 100})` }}
        />
      </div>
    );
  }
);

Progress.displayName = 'Progress';
