import os
import re
import csv
import json
import time
from typing import List, Dict, Tuple, Optional, Set
import requests
from dotenv import load_dotenv
import pandas as pd

print("Dependencies loaded successfully")

load_dotenv()

# Circuit/Spoke API configuration
CIRCUIT_API_KEY = (
    os.getenv("CIRCUIT_API_KEY")      # new name
    or os.getenv("SPOKE_API_KEY")     # fallback for existing name
    or ""
).strip()
CIRCUIT_BASE = os.getenv("CIRCUIT_BASE", "https://api.getcircuit.com/public/v0.2b").rstrip("/")

# Zoho CRM API configuration
ZOHO_ACCOUNTS_URL = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com").rstrip("/")
ZOHO_API_BASE = os.getenv("ZOHO_API_BASE", "https://www.zohoapis.com/crm/v8").rstrip("/")

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "").strip()
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "").strip()
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "").strip()

# Zoho field mappings
Z_FIELD_STREET = os.getenv("Z_FIELD_STREET", "Street_Address").strip()
Z_FIELD_ZIP = os.getenv("Z_FIELD_ZIP", "Zip_Code").strip()
Z_FIELD_STAGE = os.getenv("Z_FIELD_STAGE", "Stage").strip()

# Zoho UI URL configuration (for clickable record links)
# Set Z_ORG_ID (and optionally Z_UI_DOMAIN / Z_UI_MODULE) in your .env
Z_UI_DOMAIN = os.getenv("Z_UI_DOMAIN", "https://crm.zoho.com").rstrip("/")
Z_ORG_ID = os.getenv("Z_ORG_ID", "").strip()           # e.g., "123456789"
Z_UI_MODULE = "CustomModule5"

def make_zoho_record_url(record_id: str) -> str:
    """
    Build the Zoho CRM UI URL for a Locatings record.

    Pattern:
        <Z_UI_DOMAIN>/crm/org<Z_ORG_ID>/tab/<Z_UI_MODULE>/<record_id>

    Returns an empty string if Z_ORG_ID or record_id is missing.
    """
    if not (Z_ORG_ID and record_id):
        return ""
    return f"{Z_UI_DOMAIN}/crm/org{Z_ORG_ID}/tab/{Z_UI_MODULE}/{record_id}"

# COQL Configuration
MAX_COQL_LIMIT = 200  # Zoho's max per query
BATCH_SIZE = 20       # Number of prefixes per COQL OR clause (used by earlier versions)

print("Configuration:")
print(f"  Circuit base: {CIRCUIT_BASE}")
print(f"  Zoho base: {ZOHO_API_BASE}")
print(f"  Locatings fields: {Z_FIELD_STREET}, {Z_FIELD_ZIP}, {Z_FIELD_STAGE}")

# Validate credentials
assert ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN, "Missing Zoho credentials in .env"
if not CIRCUIT_API_KEY:
    print("WARN: CIRCUIT_API_KEY/SPOKE_API_KEY not set. Circuit/Spoke API calls will fail.")

# %% [markdown]
# ## Cell 3: Zoho OAuth – Mint Access Token

# %%
def mint_access_token() -> str:
    """Mint a fresh Zoho OAuth access token using the refresh token."""
    try:
        r = requests.post(
            f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": ZOHO_REFRESH_TOKEN,
                "client_id": ZOHO_CLIENT_ID,
                "client_secret": ZOHO_CLIENT_SECRET,
            },
            timeout=30,
        )
        r.raise_for_status()
        js = r.json()
        
        if "access_token" not in js:
            raise RuntimeError(f"Unexpected token response: {js}")
        
        token = js["access_token"]
        print(f"Access token OK: {token[:24]}...")
        return token
    except Exception as e:
        print(f"Error minting token: {e}")
        raise

# %% [markdown]
# ## Circuit/Spoke API Functions
# %%
def iter_plan_stops(plan_id: str, page_size: int = 100):
    """
    Iterate all stops in a Circuit plan with pagination.
    Handles maxPageSize parameter gracefully (some tenants reject it).
    """
    token = None
    use_max = True  # Try using maxPageSize initially
    auth = (CIRCUIT_API_KEY, "")  # Basic auth
    
    while True:
        params = {}
        if token:
            params["pageToken"] = token
        if use_max and page_size:
            params["maxPageSize"] = page_size
        
        try:
            r = requests.get(
                f"{CIRCUIT_BASE}/plans/{plan_id}/stops",
                auth=auth,
                params=params,
                timeout=60
            )
            
            # If server rejects maxPageSize, retry without it
            if r.status_code == 400 and use_max:
                use_max = False
                r = requests.get(
                    f"{CIRCUIT_BASE}/plans/{plan_id}/stops",
                    auth=auth,
                    params={"pageToken": token} if token else {},
                    timeout=60
                )
            
            r.raise_for_status()
            js = r.json()
            
            for stop in js.get("stops", []):
                yield stop
            
            token = js.get("nextPageToken")
            if not token:
                break
        except Exception as e:
            print(f"Error fetching stops: {e}")
            raise


def get_route_stops_circuit(plan_id: str, route_sid: str) -> list:
    """
    Get all stops for a specific route within a plan.
    Handles route_sid with or without 'routes/' prefix.
    """
    # Normalize route ID (handle both 'routes/xxx' and 'xxx' formats)
    if route_sid.startswith("routes/"):
        core_sid = route_sid.split("/", 1)[1]
    else:
        core_sid = route_sid
    
    accept = {core_sid, f"routes/{core_sid}"}
    
    stops = []
    for stop in iter_plan_stops(plan_id, page_size=100):
        route_info = stop.get("route") or {}
        route_id = route_info.get("id") or route_info.get("sid")
        
        if route_id in accept:
            stops.append(stop)
    
    return stops

# %%
# Regular expressions for parsing
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
HOUSE_STREET_RE = re.compile(r"^\s*(?P<house>\d+[A-Za-z\-]?)\s+(?P<street>[^,]+)", re.I)

def normalize_street_label(label: str) -> str:
    """
    Normalize street suffixes and remove punctuation for consistent matching.
    e.g., "Main Street" -> "main st", "1st Avenue" -> "1st ave"
    """
    s = label.lower()
    
    # Common street suffix normalizations
    replacements = [
        ("street", "st"), ("st.", "st"),
        ("avenue", "ave"), ("ave.", "ave"),
        ("road", "rd"), ("rd.", "rd"),
        ("boulevard", "blvd"), ("blvd.", "blvd"),
        ("parkway", "pkwy"), ("pkwy.", "pkwy"),
        ("drive", "dr"), ("dr.", "dr"),
        ("lane", "ln"), ("ln.", "ln"),
        ("court", "ct"), ("ct.", "ct"),
        ("place", "pl"), ("pl.", "pl"),
        ("circle", "cir"), ("cir.", "cir"),
        ("highway", "hwy"), ("hwy.", "hwy"),
    ]
    
    for old, new in replacements:
        s = s.replace(old, new)
    
    # Remove punctuation except hyphens (for addresses like 123-A)
    s = re.sub(r"[^\w\s\-]", "", s)
    # Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()
    
    return s


def parse_prefix(full_address: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse address into (zip5, raw_prefix, normalized_prefix).
    raw_prefix: "123 Main Street"
    normalized_prefix: "123 main st"
    """
    if not full_address:
        return (None, None, None)
    
    # Extract ZIP code
    zip_match = ZIP_RE.search(full_address)
    zip5 = zip_match.group(1) if zip_match else None
    
    # Extract house number and street
    # Take first part before comma (usually street address)
    parts = full_address.split(",")
    if not parts:
        return (zip5, None, None)
    
    street_part = parts[0].strip()
    match = HOUSE_STREET_RE.match(street_part)
    
    if not match:
        return (zip5, None, None)
    
    house = match.group('house')
    street = match.group('street').strip()
    
    raw_prefix = f"{house} {street}"
    norm_prefix = f"{house} {normalize_street_label(street)}"
    
    return (zip5, raw_prefix, norm_prefix)


def prepare_stops(stops: List[dict]) -> List[dict]:
    """
    Prepare stops for matching by extracting and normalizing addresses.
    """
    prepared = []
    
    for stop in stops:
        addr_obj = stop.get("address", {})
        
        # Try different address fields
        full_address = (
            addr_obj.get("address")
            or addr_obj.get("addressLineTwo")
            or addr_obj.get("full")
            or ""
        )
        
        business_name = (
            addr_obj.get("addressLineOne")
            or addr_obj.get("name")
            or stop.get("name")
            or ""
        )
        
        zip5, raw_prefix, norm_prefix = parse_prefix(full_address)
        
        prepared.append({
            "stopId": stop.get("id") or stop.get("sid"),
            "business": business_name,
            "address": full_address,
            "zip": zip5,
            "raw_prefix": raw_prefix,
            "norm_prefix": norm_prefix,
        })
    
    return prepared


def to_norm_prefix_from_zoho_street(street: str) -> str:
    """Normalize Zoho street text into a comparable prefix."""
    if not street:
        return ""
    match = HOUSE_STREET_RE.match(street.strip())
    if not match:
        return ""
    return (match.group("house") + " " + normalize_street_label(match.group("street"))).strip()

# %% [markdown]
# ## Cell 7: COQL Query Functions

# %%
# Cell 7 – COQL helper functions with nested OR batching

# COQL WHERE clause limits:
#   - Up to 25 criteria total in WHERE.
#   - 1 criterion used by ZIP, so we allow up to 24 street predicates.
#   - We stay under that for safety.
MAX_OR_CLAUSES = 20  # you can raise to 24 if you want


def escape_like(s: str) -> str:
    """
    Escape wildcard characters for COQL LIKE.
    Backslash, single quote, % and _ must be escaped.
    """
    return (
        s.replace("\\", "\\\\")
         .replace("'", "\\'")
         .replace("%", "\\%")
         .replace("_", "\\_")
    )


def query_prefix_from_raw(raw_prefix: str) -> str:
    """
    Shorten a raw street prefix for use in COQL LIKE.

    We keep only:
      house number + first street word

    Example:
      '55 Fair Drive' -> '55 Fair'
      '3333 Bear St Suite 142' -> '3333 Bear'
    """
    if not raw_prefix:
        return ""

    s = raw_prefix.strip()
    parts = s.split()
    if len(parts) >= 2:
        return " ".join(parts[:2])
    return s


def nest_or(clauses: list[str]) -> str:
    """
    Build a properly parenthesized OR expression:

        [A, B, C] -> "((A or B) or C)"

    COQL requires nested parentheses when you have >2 criteria.
    """
    if not clauses:
        return ""
    expr = clauses[0]
    for c in clauses[1:]:
        expr = f"({expr} or {c})"
    return expr


def _coql_query(query: str, token: str) -> list:
    """
    Low-level helper to execute a COQL query and return the 'data' list.

    We do not print 400 responses here; callers (coql_zip_batch) decide
    how to handle them.
    """
    r = requests.post(
        f"{ZOHO_API_BASE}/coql",
        headers={
            "Authorization": f"Zoho-oauthtoken {token}",
            "Content-Type": "application/json",
        },
        json={"select_query": query},
        timeout=60,
    )

    # 204 = no content / no matches
    if r.status_code == 204:
        return []

    # Raise for 4xx/5xx; caller will handle 400 specially
    r.raise_for_status()

    js = r.json()
    return js.get("data", [])


def coql_zip_batch(
    street_field: str,
    zip_field: str,
    stage_field: str,
    zip_code: str,
    raw_prefixes: list,
    token: str,
) -> list:
    """
    Query Locatings by ZIP and multiple street prefixes.

    - Batches prefixes in chunks of up to MAX_OR_CLAUSES.
    - For each chunk, builds a properly nested OR expression so COQL
      accepts more than 2 conditions.
    - Falls back to splitting the chunk on HTTP 400.
    """

    # Deduplicate prefixes for this ZIP
    unique_raw = sorted(set(raw_prefixes))
    all_rows: list[dict] = []

    def run_chunk(chunk_prefixes: list[str]) -> list[dict]:
        """
        Execute one COQL query for a list of raw prefixes.
        On 400, recursively split the chunk until we either succeed or
        isolate a single bad prefix.
        """
        if not chunk_prefixes:
            return []

        # Build street predicates
        predicates = []
        for raw in chunk_prefixes:
            q_prefix = query_prefix_from_raw(raw)
            if not q_prefix:
                continue
            escaped = escape_like(q_prefix)
            predicates.append(f"{street_field} like '{escaped}%'")

        if not predicates:
            return []

        # Build nested OR expression
        or_expr = nest_or(predicates)

        # Full COQL query
        query = (
            f"select id, {street_field} as street, {zip_field} as zip, "
            f"{stage_field} as stage "
            f"from Locatings "
            f"where ({zip_field} = '{zip_code}' and ({or_expr})) "
            f"limit 0, {MAX_COQL_LIMIT}"
        )

        try:
            return _coql_query(query, token)

        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            status = resp.status_code if resp is not None else None

            # If COQL rejects this query with 400 and we have multiple
            # prefixes, split and retry to isolate the problematic prefix.
            if status == 400 and len(chunk_prefixes) > 1:
                mid = len(chunk_prefixes) // 2
                left = run_chunk(chunk_prefixes[:mid])
                right = run_chunk(chunk_prefixes[mid:])
                return left + right

            if status == 400 and len(chunk_prefixes) == 1:
                bad = chunk_prefixes[0]
                # Optional: uncomment to log the specific bad prefix
                # print(f"Skipping prefix '{bad}' in ZIP {zip_code} due to persistent COQL 400.")
                return []

            # Any non-400 HTTPError is treated as fatal
            raise

    # Process prefixes in batches to stay under the 25-criteria limit
    for i in range(0, len(unique_raw), MAX_OR_CLAUSES):
        batch = unique_raw[i : i + MAX_OR_CLAUSES]
        rows = run_chunk(batch)
        all_rows.extend(rows)

        # For max speed, no sleep. If you ever see rate-limit errors, add a tiny delay here.

    return all_rows


print("COQL query functions loaded (nested OR batching).")

# %% [markdown]
# ## Cell 8: Batch Matching Process

# %%
def batch_match_stops(prepared_stops: List[dict], token: str) -> Tuple[list, list, list]:
    """
    Match prepared stops against Zoho CRM.
    Returns (present, ambiguous, not_found) lists.
    """
    # Group stops by ZIP code
    zip_to_prefixes: Dict[str, Set[str]] = {}  # {zip: set(raw_prefixes)}
    index: Dict[Tuple[str, str], List[int]] = {}  # {(zip, norm_prefix): [stop_indices]}
    
    for i, stop in enumerate(prepared_stops):
        if stop["zip"] and stop["raw_prefix"] and stop["norm_prefix"]:
            z = stop["zip"]
            zip_to_prefixes.setdefault(z, set()).add(stop["raw_prefix"])
            index.setdefault((z, stop["norm_prefix"]), []).append(i)
    
    print(f"Processing {len(zip_to_prefixes)} ZIP codes, "
          f"{sum(len(v) for v in zip_to_prefixes.values())} unique prefixes\n")
    
    # Query Zoho in batches and build a lookup by (zip, norm_prefix)
    by_zip_norm: Dict[str, List[Tuple[str, dict]]] = {}  # {zip: [(norm_prefix, zoho_row), ...]}
    
    for zip_code, raw_prefixes in zip_to_prefixes.items():
        print(f"Querying ZIP {zip_code}: {len(raw_prefixes)} prefixes...", end="")
        rows = coql_zip_batch(
            Z_FIELD_STREET,
            Z_FIELD_ZIP,
            Z_FIELD_STAGE,
            zip_code,
            sorted(raw_prefixes),
            token,
        )
        
        # Process results
        bucket: List[Tuple[str, dict]] = []
        for row in rows:
            zoho_street = row.get("street") or ""
            norm_prefix = to_norm_prefix_from_zoho_street(zoho_street)
            if norm_prefix:
                bucket.append((norm_prefix, row))
        
        by_zip_norm[zip_code] = bucket
        print(f" found {len(rows)} records")
    
    # Match stops to Zoho records
    present: List[dict] = []
    ambiguous: List[dict] = []
    not_found: List[dict] = []
    
    for stop in prepared_stops:
        z = stop["zip"]
        np = stop["norm_prefix"]
        
        # Skip if we can't query this stop
        if not (z and np):
            not_found.append({
                **stop,
                "match_type": "not_queryable",
            })
            continue
        
        # Find matching Zoho records
        candidates: List[dict] = []
        for zoho_norm, zoho_record in by_zip_norm.get(z, []):
            # Check if Zoho normalized prefix starts with our normalized prefix
            if zoho_norm.startswith(np):
                candidates.append(zoho_record)
        
        # Classify the match
        if len(candidates) == 1:
            # Exact match found
            record = candidates[0]
            zoho_id = record.get("id")
            present.append({
                **stop,
                "match_type": "present",
                "zoho_id": zoho_id,
                "zoho_street": record.get("street"),
                "zoho_stage": record.get("stage"),
                "zoho_url": make_zoho_record_url(zoho_id),
                "candidate_count": 1,
            })
        elif len(candidates) > 1:
            # Multiple matches found
            ambiguous.append({
                **stop,
                "match_type": "ambiguous",
                "candidate_count": len(candidates),
                # For ambiguous rows we do NOT attach a specific zoho_id/url,
                # since there is more than one candidate.
            })
        else:
            # No match found
            not_found.append({
                **stop,
                "match_type": "not_found",
            })
    
    return present, ambiguous, not_found

def run_matching(plan_id: str, route_sid: str):
    """
    High-level pipeline: Circuit -> prepared stops -> Zoho -> match buckets.

    Args:
        plan_id: Circuit plan ID.
        route_sid: Circuit route SID (with or without 'routes/' prefix).

    Returns:
        (present, ambiguous, not_found, prepared, stops)
    """
    # 1) Fetch stops from Circuit / Spoke
    stops = get_route_stops_circuit(plan_id, route_sid)

    # 2) Prepare addresses for matching
    prepared = prepare_stops(stops)

    # 3) Get a fresh Zoho OAuth access token
    token = mint_access_token()

    # 4) Batch match against Zoho via COQL
    present, ambiguous, not_found = batch_match_stops(prepared, token)

    return present, ambiguous, not_found, prepared, stops