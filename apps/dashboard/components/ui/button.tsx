'use client';

import { forwardRef, type ButtonHTMLAttributes } from 'react';

import { cn } from '@/lib/utils';

type ButtonVariant = 'default' | 'ghost' | 'destructive';
type ButtonSize = 'md' | 'sm';

const VARIANT_CLASSNAMES: Record<ButtonVariant, string> = {
  default:
    'bg-slate-100 text-slate-900 hover:bg-slate-200 transition-colors border border-slate-800',
  ghost: 'bg-transparent border border-slate-700 text-slate-100 hover:bg-slate-800',
  destructive:
    'bg-red-600 text-white hover:bg-red-500 transition-colors border border-red-500 focus-visible:ring-red-400'
};

const SIZE_CLASSNAMES: Record<ButtonSize, string> = {
  md: 'h-10 px-4 py-2 text-sm rounded-md',
  sm: 'h-8 px-3 py-1 text-xs rounded-md'
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = 'default', size = 'md', ...props }, ref) => {
    return (
      <button
        ref={ref}
        className={cn(
          'inline-flex items-center justify-center font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 disabled:opacity-60 disabled:pointer-events-none',
          VARIANT_CLASSNAMES[variant],
          SIZE_CLASSNAMES[size],
          className
        )}
        {...props}
      />
    );
  }
);

Button.displayName = 'Button';
