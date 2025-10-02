interface BucketState {
  tokens: number;
  lastRefill: number;
}

export interface RateLimitOptions {
  capacity: number;
  windowMs: number;
  refillRate?: number;
}

export interface RateLimitStats {
  totalBuckets: number;
  activeBuckets: number;
}

export class TokenBucketLimiter {
  private readonly buckets = new Map<string, BucketState>();
  private readonly capacity: number;
  private readonly windowMs: number;
  private readonly refillRate: number;
  private cleanupCounter = 0;
  private readonly cleanupThreshold = 1000;

  constructor(options: RateLimitOptions) {
    if (options.capacity <= 0) {
      throw new Error('TokenBucketLimiter: capacity must be greater than zero');
    }
    if (options.windowMs <= 0) {
      throw new Error('TokenBucketLimiter: windowMs must be greater than zero');
    }
    this.capacity = Math.floor(options.capacity);
    this.windowMs = Math.floor(options.windowMs);
    const refill = options.refillRate ?? this.capacity;
    this.refillRate = refill <= 0 ? this.capacity : Math.floor(refill);
  }

  public take(identifier: string): boolean {
    const now = Date.now();
    const bucket = this.getBucket(identifier, now);

    this.refill(bucket, now);

    if (bucket.tokens < 1) {
      this.buckets.set(identifier, bucket);
      return false;
    }

    bucket.tokens -= 1;
    this.buckets.set(identifier, bucket);

    this.maybeCleanup(now);
    return true;
  }

  public reset(identifier: string): void {
    this.buckets.delete(identifier);
  }

  public getStats(): RateLimitStats {
    let activeBuckets = 0;
    for (const bucket of this.buckets.values()) {
      if (bucket.tokens < this.capacity) {
        activeBuckets += 1;
      }
    }
    return {
      totalBuckets: this.buckets.size,
      activeBuckets
    };
  }

  public cleanup(expirationMs = this.windowMs * 5): number {
    const now = Date.now();
    let removed = 0;
    for (const [key, bucket] of this.buckets.entries()) {
      if (now - bucket.lastRefill > expirationMs) {
        this.buckets.delete(key);
        removed += 1;
      }
    }
    return removed;
  }

  private getBucket(identifier: string, now: number): BucketState {
    const existing = this.buckets.get(identifier);
    if (existing) {
      return existing;
    }
    return { tokens: this.capacity, lastRefill: now };
  }

  private refill(bucket: BucketState, now: number): void {
    const elapsed = now - bucket.lastRefill;
    if (elapsed <= 0) {
      return;
    }

    const tokensToAdd = Math.floor((elapsed / this.windowMs) * this.refillRate);
    if (tokensToAdd > 0) {
      bucket.tokens = Math.min(this.capacity, bucket.tokens + tokensToAdd);
      bucket.lastRefill = now;
    }
  }

  private maybeCleanup(_now: number): void {
    this.cleanupCounter += 1;
    if (this.cleanupCounter < this.cleanupThreshold) {
      return;
    }
    this.cleanupCounter = 0;
    this.cleanup();
  }
}
