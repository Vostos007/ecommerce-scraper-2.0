-- Jobs: основная таблица job'ов
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'queued',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    options JSONB,
    error_message TEXT,
    
    -- Метрики
    total_urls INTEGER DEFAULT 0,
    success_urls INTEGER DEFAULT 0,
    failed_urls INTEGER DEFAULT 0,
    
    -- Бюджеты
    traffic_mb_used NUMERIC(10,2) DEFAULT 0,
    residential_mb_used NUMERIC(10,2) DEFAULT 0,
    
    CONSTRAINT chk_status CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'))
);

CREATE INDEX idx_jobs_domain ON jobs(domain);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_created_at ON jobs(created_at DESC);

-- Pages: результаты по каждому URL
CREATE TABLE IF NOT EXISTS pages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    final_url TEXT,
    http_status INTEGER,
    fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Content
    title TEXT,
    h1 TEXT,
    content_hash VARCHAR(64),
    bytes_in INTEGER DEFAULT 0,
    
    -- Structured data
    data_full JSONB,
    data_seo JSONB,
    
    -- Errors
    error_class VARCHAR(100),
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    
    -- Strategy tracking
    strategy_used VARCHAR(50),
    proxy_used VARCHAR(255)
);

CREATE INDEX idx_pages_job_id ON pages(job_id);
CREATE INDEX idx_pages_url ON pages(url);
CREATE INDEX idx_pages_http_status ON pages(http_status);

-- Snapshots: для diff
CREATE TABLE IF NOT EXISTS snapshots (
    url TEXT PRIMARY KEY,
    domain VARCHAR(255) NOT NULL,
    last_hash VARCHAR(64),
    last_data JSONB,
    last_crawl_at TIMESTAMP WITH TIME ZONE,
    last_job_id UUID REFERENCES jobs(id)
);

CREATE INDEX idx_snapshots_domain ON snapshots(domain);
CREATE INDEX idx_snapshots_last_crawl_at ON snapshots(last_crawl_at DESC);

-- Exports: артефакты экспорта
CREATE TABLE IF NOT EXISTS exports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    type VARCHAR(20) NOT NULL,
    format VARCHAR(10) NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    CONSTRAINT chk_type CHECK (type IN ('full', 'seo', 'diff')),
    CONSTRAINT chk_format CHECK (format IN ('csv', 'xlsx', 'json'))
);

CREATE INDEX idx_exports_job_id ON exports(job_id);
CREATE INDEX idx_exports_type ON exports(type);

-- Metrics: временные ряды метрик
CREATE TABLE IF NOT EXISTS metrics (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    value NUMERIC(12,4),
    labels JSONB,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_metrics_job_id ON metrics(job_id);
CREATE INDEX idx_metrics_name ON metrics(name);
CREATE INDEX idx_metrics_timestamp ON metrics(timestamp DESC);