'use client';

import { forwardRef } from 'react';
import type { InputHTMLAttributes } from 'react';

import { cn } from '@/lib/utils';

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
  size?: 'sm' | 'md' | 'lg';
  error?: boolean;
}

const sizeClasses: Record<NonNullable<InputProps['size']>, string> = {
  sm: 'h-8 text-sm px-3',
  md: 'h-10 text-sm px-3',
  lg: 'h-12 text-base px-4'
};

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, type = 'text', size = 'md', error = false, ...props }, ref) => {
    return (
      <input
        ref={ref}
        type={type}
        className={cn(
          'flex w-full rounded-md border bg-background text-foreground shadow-sm transition-colors',
          'placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          'disabled:cursor-not-allowed disabled:opacity-50',
          sizeClasses[size],
          error ? 'border-destructive focus-visible:ring-destructive' : 'border-border',
          className
        )}
        aria-invalid={error}
        {...props}
      />
    );
  }
);

Input.displayName = 'Input';
