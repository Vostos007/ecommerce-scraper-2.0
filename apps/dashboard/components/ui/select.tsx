'use client';

import { forwardRef } from 'react';
import type { SelectHTMLAttributes } from 'react';

import { cn } from '@/lib/utils';
import { ChevronDown } from 'lucide-react';

export interface SelectOption<TValue extends string = string> {
  label: string;
  value: TValue;
  disabled?: boolean;
}

export interface SelectProps<TValue extends string = string>
  extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size'> {
  options: Array<SelectOption<TValue>>;
  variant?: 'default' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
}

const sizeClasses: Record<NonNullable<SelectProps['size']>, string> = {
  sm: 'h-8 text-sm px-3',
  md: 'h-10 text-sm px-3',
  lg: 'h-12 text-base px-4'
};

const variantClasses: Record<NonNullable<SelectProps['variant']>, string> = {
  default: 'border-border bg-background text-foreground',
  ghost: 'border-transparent bg-transparent text-foreground'
};

export const Select = forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, options, variant = 'default', size = 'md', disabled, ...props }, ref) => {
    return (
      <div className="relative">
        <select
          ref={ref}
          disabled={disabled}
          className={cn(
            'flex w-full appearance-none rounded-md border shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            'disabled:cursor-not-allowed disabled:opacity-50',
            sizeClasses[size],
            variantClasses[variant],
            className
          )}
          {...props}
        >
          {options.map((option) => (
            <option key={option.value} value={option.value} disabled={option.disabled}>
              {option.label}
            </option>
          ))}
        </select>
        <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-muted-foreground">
          <ChevronDown className="h-4 w-4" aria-hidden />
        </span>
      </div>
    );
  }
);

Select.displayName = 'Select';
