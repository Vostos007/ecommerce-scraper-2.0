"""Sites endpoints."""
import json
import os
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from ..dependencies import get_db
from ..models import SiteSummary
from database.manager import DatabaseManager


router = APIRouter()


SCRIPT_ALLOWLIST = {
    'atmospherestore.ru': 'atmosphere_fast_export',
    'sittingknitting.ru': 'cityknitting_fast_export',
    'knitshop.ru': 'knitshop_fast_export',
    'ili-ili.com': 'ili_ili_fast_export',
    'triskeli.ru': 'triskeli_fast_export',
    'manefa.ru': 'manefa_fast_export'
}


def read_config() -> List[dict]:
    config_path = Path(__file__).parent.parent.parent.parent / 'config' / 'sites.json'
    with open(config_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data.get('sites', [])


def sanitize_site(domain: str) -> str | None:
    if not isinstance(domain, str):
        return None
    value = domain.strip().lower()
    if not value or '..' in value or '/' in value or '\\' in value:
        return None
    without_www = value[4:] if value.startswith('www.') else value
    return without_www.lower()


def compute_export_metadata(domain: str) -> dict:
    try:
        export_dir = Path(__file__).parent.parent.parent.parent / 'data' / 'sites' / domain / 'exports'
        excel_path = export_dir / 'latest.xlsx'
        json_path = export_dir / 'latest.json'

        stat = None
        if excel_path.exists():
            stat = excel_path.stat()
        elif json_path.exists():
            stat = json_path.stat()

        if stat:
            map_details = resolve_map_metadata(domain)
            return {
                'lastExport': stat.st_mtime_ns / 1e9,  # timestamp
                'exportStatus': 'ready',
                **map_details
            }
        else:
            map_details = resolve_map_metadata(domain)
            return {
                'lastExport': None,
                'exportStatus': 'missing_export',
                **map_details
            }
    except Exception:
        map_details = resolve_map_metadata(domain)
        return {
            'lastExport': None,
            'exportStatus': 'unknown',
            **map_details
        }


def resolve_map_metadata(domain: str) -> dict:
    try:
        active = get_active_map_file(domain)
        if not active:
            return {
                'mapFile': None,
                'mapStatus': 'missing',
                'mapLastModified': None,
                'mapLinkCount': None
            }

        # For simplicity, assume available if exists
        return {
            'mapFile': active['fileName'],
            'mapStatus': 'available',
            'mapLastModified': active['modified'],
            'mapLinkCount': active.get('linkCount')
        }
    except Exception:
        return {
            'mapFile': None,
            'mapStatus': 'missing',
            'mapLastModified': None,
            'mapLinkCount': None
        }


def get_active_map_file(domain: str) -> dict | None:
    # Simplified: just check for the canonical map file
    canonical_name = f"{domain}.URL-map.json"
    map_path = Path(__file__).parent.parent.parent.parent / 'data' / 'sites' / domain / canonical_name
    if map_path.exists():
        stat = map_path.stat()
        try:
            with open(map_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            link_count = len(data.get('links', [])) if isinstance(data, dict) else None
            return {
                'filePath': str(map_path),
                'fileName': canonical_name,
                'modified': stat.st_mtime_ns / 1e9,
                'linkCount': link_count
            }
        except Exception:
            return {
                'filePath': str(map_path),
                'fileName': canonical_name,
                'modified': stat.st_mtime_ns / 1e9,
                'linkCount': None
            }
    return None


def get_site_summaries() -> List[SiteSummary]:
    sites = read_config()
    summaries = []

    for site in sites:
        domain = sanitize_site(site['domain'])
        if not domain:
            continue
        script = SCRIPT_ALLOWLIST.get(domain)
        if not script:
            continue

        metadata = compute_export_metadata(domain)
        export_status = (
            'missing_map' if metadata['exportStatus'] == 'ready' and metadata['mapStatus'] == 'missing'
            else metadata['exportStatus']
        )

        summaries.append(SiteSummary(
            domain=domain,
            name=site['name'],
            lastExport=metadata['lastExport'],
            status=export_status,
            script=script,
            mapFile=metadata['mapFile'],
            mapStatus=metadata['mapStatus'],
            mapLastModified=metadata['mapLastModified'],
            mapLinkCount=metadata['mapLinkCount']
        ))

    return summaries


@router.get("/", response_model=List[SiteSummary])
async def list_sites():
    """
    List all configured sites with their status.
    
    Returns site summaries including export status, map status, etc.
    """
    return get_site_summaries()


@router.get("/{site}", response_model=SiteSummary)
async def get_site(site: str):
    """
    Get detailed information for a specific site.
    
    Args:
        site: Site domain
        
    Returns:
        SiteSummary: Detailed site information
        
    Raises:
        HTTPException: 404 if site not found
    """
    summaries = get_site_summaries()
    for summary in summaries:
        if summary.domain == site:
            return summary
    raise HTTPException(status_code=404, detail="Site not found")