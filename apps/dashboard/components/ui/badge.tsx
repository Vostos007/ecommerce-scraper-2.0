import { forwardRef, type HTMLAttributes } from 'react';

import { cn } from '@/lib/utils';

const VARIANTS = {
  default: 'bg-slate-800 text-slate-100 border border-slate-600',
  primary: 'bg-blue-500/20 text-blue-100 border border-blue-500/60',
  success: 'bg-emerald-500/20 text-emerald-100 border border-emerald-500/60',
  warning: 'bg-amber-500/20 text-amber-100 border border-amber-500/60',
  error: 'bg-rose-500/20 text-rose-100 border border-rose-500/60',
  outline: 'bg-transparent text-slate-200 border border-slate-600'
};

const SIZES = {
  sm: 'text-[10px] px-1.5 py-0.5 rounded',
  md: 'text-xs px-2 py-0.5 rounded-md',
  lg: 'text-sm px-3 py-1 rounded-lg'
};

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: keyof typeof VARIANTS;
  size?: keyof typeof SIZES;
  disabled?: boolean;
}

export const Badge = forwardRef<HTMLSpanElement, BadgeProps>(
  ({ className, variant = 'default', size = 'md', disabled = false, ...props }, ref) => (
    <span
      ref={ref}
      className={cn(
        'inline-flex items-center gap-1 font-medium uppercase tracking-wide select-none transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-500',
        disabled && 'opacity-60',
        VARIANTS[variant],
        SIZES[size],
        className
      )}
      aria-disabled={disabled || undefined}
      {...props}
    />
  )
);

Badge.displayName = 'Badge';
