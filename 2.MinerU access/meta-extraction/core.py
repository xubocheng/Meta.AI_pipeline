"""
Core utilities for meta-extraction system.

Contains:
- Rate limiter
- LLM client wrapper
- File I/O utilities
- Text processing functions
- Anchor detection
- Outcome normalization
"""

from __future__ import annotations

import os
import re
import csv
import json
import time
import hashlib
import logging
import threading
import random
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class Anchor:
    """Data anchor (table or text block) with location metadata."""
    study: str
    doc_hash: str
    anchor_id: str
    anchor_title: str
    source_mode: str  # 'table' | 'text'
    evidence_locator: str
    preview: str
    char_span: Tuple[int, int]


# ============================================================================
# Constants
# ============================================================================

# Valid file extensions
VALID_EXTS = (".md", ".markdown", ".txt", ".html")

# Canonical outcome set
OUTCOME_CANON_SET = {"ADG", "ADFI", "FCR", "G/F"}

# Outcome synonyms (loaded from config in practice)
OUTCOME_SYNONYMS: Dict[str, List[str]] = {
    "ADG":  ["adg", "average daily gain", "daily gain", "dwg", "平均日增重", "日增重"],
    "ADFI": ["adfi", "average daily feed intake", "feed intake", "日采食量", "平均日采食量"],
    "FCR":  ["fcr", "feed conversion ratio", "feed:gain", "feed/gain", "料肉比", "f:g"],
    "G/F":  ["g/f", "g:f", "gain:feed", "gain/feed", "feed efficiency", "增重:采食", "效率"],
}

# Regular expressions for table detection
MD_TABLE_BLOCK_RE = re.compile(
    r"(?ms)^\s*\|.*\|\s*\n\s*\|(?:[-:]+\|)+\s*\n(?:\s*\|.*\|\s*\n)+"
)
HTML_TABLE_BLOCK_RE = re.compile(r"(?is)<table\b.*?>.*?</table>")


# ============================================================================
# Rate Limiter
# ============================================================================

class RateLimiter:
    """Thread-safe rate limiter for API calls."""
    
    def __init__(self, cap_per_min: int):
        self.cap = max(1, int(cap_per_min))
        self.lock = threading.Lock()
        self.times: List[float] = []
    
    def acquire(self):
        """Block until rate limit allows another call."""
        with self.lock:
            now = time.time()
            one_min_ago = now - 60.0
            self.times = [t for t in self.times if t > one_min_ago]
            
            if len(self.times) < self.cap:
                self.times.append(now)
                return
            
            wait_s = 60.0 - (now - self.times[0])
        
        time.sleep(max(0.0, wait_s))
        self.acquire()


# ============================================================================
# LLM Client
# ============================================================================

class DeepSeekClient:
    """DeepSeek API client with retry logic and usage logging."""
    
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        api_path: str = "/v1/chat/completions",
        model: str = "deepseek-chat",
        temperature: float = 0.0,
        timeout_s: int = 150,
        max_tokens: int = 3000,
        rate_limiter: Optional[RateLimiter] = None,
        usage_log_csv: Optional[str] = None,
        retries: int = 2,
        backoff_base: float = 1.6,
        initial_delay: float = 0.6,
        backoff_max: float = 20.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.api_path = api_path
        self.model = model
        self.temperature = float(temperature)
        self.timeout_s = int(timeout_s)
        self.max_tokens = int(max_tokens)
        self.rate_limiter = rate_limiter or RateLimiter(60)
        self.usage_log_csv = usage_log_csv
        
        self.retries = int(max(0, retries))
        self.backoff_base = float(backoff_base)
        self.initial_delay = float(initial_delay)
        self.backoff_max = float(backoff_max)
        
        # Initialize usage log
        if self.usage_log_csv and not os.path.exists(self.usage_log_csv):
            os.makedirs(os.path.dirname(self.usage_log_csv), exist_ok=True)
            with open(self.usage_log_csv, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow([
                    "time", "stage", "study", "anchor_id", "prompt_len", "response_len",
                    "model", "success", "attempts", "status_code", "error_excerpt"
                ])
        
        if not self.api_key or self.api_key == "YOUR_API_KEY_HERE":
            logging.warning("DeepSeek API key not configured. Calls will fail.")
    
    def _log_usage(self, stage: str, study: str, anchor_id: str,
                   prompt_len: int, response_len: int,
                   success: bool, attempts: int,
                   status_code: Optional[int], error_excerpt: str):
        """Log API usage to CSV."""
        if not self.usage_log_csv:
            return
        
        with open(self.usage_log_csv, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([
                datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
                stage, study, anchor_id,
                prompt_len, response_len, self.model,
                "1" if success else "0",
                attempts,
                status_code if status_code is not None else "",
                (error_excerpt or "")[:300]
            ])
    
    def chat2(self, system_prompt: str, user_prompt: str,
              stage: str = "", study: str = "", anchor_id: str = "") -> Tuple[str, Dict[str, Any]]:
        """
        Make a chat completion request with retry logic.
        
        Returns:
            Tuple[str, Dict]: (response_text, metadata)
                metadata contains: success, attempts, status_code, error, prompt_len, response_len
        """
        url = f"{self.base_url}{self.api_path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        
        prompt_len = len(user_prompt)
        attempts = 0
        last_err = ""
        last_status = None
        text = ""
        
        for attempt in range(self.retries + 1):
            attempts = attempt + 1
            self.rate_limiter.acquire()
            
            try:
                resp = requests.post(
                    url, 
                    headers=headers, 
                    data=json.dumps(payload), 
                    timeout=self.timeout_s
                )
                last_status = resp.status_code
                
                if not (200 <= resp.status_code < 300):
                    err_excerpt = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    logging.warning(f"[LLM][{stage}] HTTP error on attempt {attempts}: {err_excerpt}")
                    
                    # Retry on server errors or rate limit
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                        delay = min(self.backoff_max, self.initial_delay * (self.backoff_base ** attempt))
                        delay += random.uniform(0, 0.3)
                        time.sleep(delay)
                        continue
                    
                    last_err = err_excerpt
                    break
                
                data = resp.json()
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                self._log_usage(stage, study, anchor_id, prompt_len, len(text),
                               success=True, attempts=attempts, status_code=last_status, 
                               error_excerpt="")
                
                return text, {
                    "success": True, 
                    "attempts": attempts, 
                    "status_code": last_status,
                    "error": "", 
                    "prompt_len": prompt_len, 
                    "response_len": len(text)
                }
            
            except requests.exceptions.Timeout as e:
                last_err = f"Timeout: {str(e)}"
                logging.warning(f"[LLM][{stage}] Timeout on attempt {attempts}: {last_err}")
                if attempt < self.retries:
                    delay = min(self.backoff_max, self.initial_delay * (self.backoff_base ** attempt))
                    delay += random.uniform(0, 0.3)
                    time.sleep(delay)
                    continue
                break
            
            except Exception as e:
                last_err = f"Exception: {str(e)}"
                logging.exception(f"[LLM][{stage}] Exception on attempt {attempts}: {last_err}")
                if attempt < self.retries:
                    delay = min(self.backoff_max, self.initial_delay * (self.backoff_base ** attempt))
                    delay += random.uniform(0, 0.3)
                    time.sleep(delay)
                    continue
                break
        
        # All retries failed
        self._log_usage(stage, study, anchor_id, prompt_len, len(text),
                       success=False, attempts=attempts, status_code=last_status, 
                       error_excerpt=last_err)
        
        return text, {
            "success": False, 
            "attempts": attempts, 
            "status_code": last_status,
            "error": last_err, 
            "prompt_len": prompt_len, 
            "response_len": len(text)
        }


# ============================================================================
# File I/O Utilities
# ============================================================================

def md5_of_file(path: str, chunk_size: int = 1 << 20) -> str:
    """Calculate MD5 hash of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_files(in_dir: str, exts: Tuple[str, ...] = VALID_EXTS) -> List[str]:
    """Recursively scan directory for files with valid extensions."""
    files: List[str] = []
    for root, _, names in os.walk(in_dir):
        for n in names:
            if n.lower().endswith(exts):
                files.append(os.path.join(root, n))
    return sorted(files)


def read_text(path: str, encoding: str = "utf-8") -> str:
    """Read text file with fallback encoding."""
    with open(path, "r", encoding=encoding, errors="ignore") as f:
        return f.read()


def guess_study_id(path: str) -> str:
    """Extract study ID from filename."""
    base = os.path.basename(path)
    stem, _ = os.path.splitext(base)
    return stem


def doc_hash_text(text: str) -> str:
    """Calculate MD5 hash of text content."""
    return hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()


# ============================================================================
# Text Processing
# ============================================================================

def slice_by_chars(s: str, max_chars: int) -> str:
    """Truncate string to maximum character length."""
    return s if len(s) <= max_chars else s[:max_chars]


def expand_context(text: str, start: int, end: int, pad: int, limit_chars: int) -> str:
    """
    Expand context window around a span with padding.
    
    Args:
        text: Full text
        start: Span start position
        end: Span end position
        pad: Padding characters on each side
        limit_chars: Maximum output length
    
    Returns:
        Expanded context string
    """
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    if s >= e:
        return slice_by_chars(text, limit_chars)
    return slice_by_chars(text[s:e], limit_chars)


def has_numeric_signal(s: str) -> bool:
    """Check if string contains numeric data signals."""
    s_low = s.lower()
    if any(t in s_low for t in ["±", " sd", " se", " sem", " ci", " n="]):
        return True
    return bool(re.search(r"\d", s))


# ============================================================================
# Outcome Normalization
# ============================================================================

def normalize_outcome_name(name: str, synonyms: Dict[str, List[str]] = OUTCOME_SYNONYMS) -> str:
    """
    Normalize outcome name to canonical form.
    
    Args:
        name: Raw outcome name
        synonyms: Synonym dictionary
    
    Returns:
        Canonical outcome name or original if no match
    """
    if not name:
        return ""
    
    raw = name.strip().lower()
    
    # Exact match to canonical
    for canon, syns in synonyms.items():
        if raw == canon.lower():
            return canon
    
    # Exact match to synonym
    for canon, syns in synonyms.items():
        for s in syns:
            if raw == s.lower():
                return canon
    
    # Substring match
    for canon, syns in synonyms.items():
        if canon.lower() in raw:
            return canon
        for s in syns:
            if s.lower() in raw:
                return canon
    
    return name


def active_anchor_keywords(
    filtered_canon: List[str],
    default_keywords: List[str],
    synonyms: Dict[str, List[str]] = OUTCOME_SYNONYMS
) -> List[str]:
    """
    Generate active keywords for anchor detection based on filtered outcomes.
    
    Args:
        filtered_canon: List of canonical outcomes to extract
        default_keywords: Default keyword list
        synonyms: Synonym dictionary
    
    Returns:
        List of active keywords
    """
    if not filtered_canon:
        return default_keywords
    
    keys = set()
    for k in filtered_canon:
        keys.add(k.lower())
        for syn in synonyms.get(k, []):
            keys.add(syn.lower())
    
    # Always include table indicators
        keys.update(["table", "表", "结果"])
    return sorted(keys)


# ============================================================================
# Anchor Detection
# ============================================================================

def overlap_ratio(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    """Calculate overlap ratio between two spans."""
    s1, e1 = a
    s2, e2 = b
    inter = max(0, min(e1, e2) - max(s1, s2))
    if inter == 0:
        return 0.0
    union = max(e1, e2) - min(s1, s2)
    return inter / union if union > 0 else 0.0


def suppress_overlaps(anchors: List[Anchor], text: str, thr: float = 0.6) -> List[Anchor]:
    """
    Suppress overlapping anchors, keeping higher-scoring ones.
    
    Scoring priority:
    1. Digit count
    2. Table mode bonus (+2.0)
    
    Args:
        anchors: List of anchors to filter
        text: Full text for scoring
        thr: Overlap ratio threshold
    
    Returns:
        Filtered anchor list
    """
    def score(anc: Anchor) -> float:
        span = text[anc.char_span[0]:anc.char_span[1]]
        digit_cnt = len(re.findall(r"\d", span))
        bonus = 2.0 if anc.source_mode == "table" else 0.0
        return digit_cnt + bonus
    
    anchors = sorted(anchors, key=lambda a: a.char_span[0])
    kept: List[Anchor] = []
    
    for a in anchors:
        if not kept:
            kept.append(a)
            continue
        
        last = kept[-1]
        if overlap_ratio(a.char_span, last.char_span) > thr:
            # Replace if new anchor has higher score
            if score(a) > score(last):
                kept[-1] = a
        else:
            kept.append(a)
    
    return kept


def find_anchors(
    text: str,
    max_anchors: int,
    keywords: List[str],
    enable_overlap_suppression: bool = True,
    overlap_threshold: float = 0.6
) -> List[Anchor]:
    """
    Find data anchors (tables and text blocks) in document.
    
    Priority:
    1. Markdown tables (highest)
    2. HTML tables
    3. Text paragraphs (with keyword matching)
    
    Args:
        text: Full document text
        max_anchors: Maximum anchors to return
        keywords: Active keywords for text matching
        enable_overlap_suppression: Apply overlap suppression
        overlap_threshold: Overlap ratio threshold
    
    Returns:
        List of Anchor objects
    """
    anchors: List[Anchor] = []
    h = doc_hash_text(text)
    
    # A) Markdown tables (highest priority)
    for i, m in enumerate(MD_TABLE_BLOCK_RE.finditer(text), start=1):
        block = m.group(0)
        span = m.span(0)
        anchors.append(Anchor(
            study="", 
            doc_hash=h, 
            anchor_id=f"MTBL{i}", 
            anchor_title="TABLE Markdown", 
            source_mode="table",
            evidence_locator=f"md_table_block_{i}", 
            preview=block[:300], 
            char_span=span
        ))
        if len(anchors) >= max_anchors:
            break
    
    # B) HTML tables
    if len(anchors) < max_anchors:
        for j, m in enumerate(HTML_TABLE_BLOCK_RE.finditer(text), start=1):
            block = m.group(0)
            span = m.span(0)
            anchors.append(Anchor(
                study="", 
                doc_hash=h, 
                anchor_id=f"HTBL{j}", 
                anchor_title="TABLE HTML", 
                source_mode="table",
                evidence_locator=f"html_table_block_{j}", 
                preview=(block[:300]).replace("\n", " ")[:300], 
                char_span=span
            ))
            if len(anchors) >= max_anchors:
                break
    
    # C) Text paragraphs (keyword-based)
    if len(anchors) < max_anchors:
        paras = re.split(r"\n{2,}", text)
        p_cursor = 0
        kset = set(keywords)
        
        for k, para in enumerate(paras, start=1):
            lower = para.lower()
            if any(kw in lower for kw in kset) and len(para.strip()) > 50:
                start = text.find(para, p_cursor)
                end = start + len(para)
                p_cursor = end
                
                # Extract first line as title
                title = para.strip().split("\n", 1)[0][:60]
                
                anchors.append(Anchor(
                    study="", 
                    doc_hash=h, 
                    anchor_id=f"TXT{k}", 
                    anchor_title=title, 
                    source_mode="text",
                    evidence_locator=f"paragraph_{k}", 
                    preview=para.strip()[:300], 
                    char_span=(start, end)
                ))
                
                if len(anchors) >= max_anchors:
                    break
    
    # Overlap suppression (optional)
    if enable_overlap_suppression and len(anchors) > 0:
        anchors = suppress_overlaps(anchors, text, thr=overlap_threshold)
    
    return anchors


# ============================================================================
# JSON Extraction
# ============================================================================

def extract_json_objects(text: str) -> List[Dict[str, Any]]:
    """
    Extract JSON objects from LLM response.
    
    Handles:
    - Code blocks:
    ```json {...}
    ```
    - Inline JSON: {...}
    - JSON arrays: [...]
    
    Returns:
        List of extracted dictionaries
    """
    if not text:
        return []
    
    cands: List[str] = []
    
    # Extract from code blocks
    cands += re.findall(r"(?:json)?\s*({.?}|$$.?", text, flags=re.S)
    
    # Extract inline JSON
    cands += re.findall(r"(\{.*\}|$$.*$$)", text, flags=re.S)
    
    out: List[Dict[str, Any]] = []
    for c in cands:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                out.append(obj)
            elif isinstance(obj, list):
                out += [it for it in obj if isinstance(it, dict)]
        except Exception:
            continue
    
    return out


# ============================================================================
# Data Validation
# ============================================================================

def is_valid_numeric_value(val) -> bool:
    """
    Strict numeric value validation.
    
    Returns False if:
    - Value is None, NaN, or empty
    - Value is invalid marker (na, n/a, null, etc.)
    - Value is not a finite number
    """
    if pd.isna(val):
        return False
    
    val_str = str(val).strip().lower()
    invalid_markers = ["", "na", "n/a", "nan", "null", "none", "--", "...", "?", "nd"]
    if val_str in invalid_markers:
        return False
    
    try:
        num = float(val_str)
        if not np.isfinite(num):
            return False
        return True
    except (ValueError, TypeError):
        return False


# ============================================================================
# DataFrame Utilities
# ============================================================================

def normalize_for_csv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize DataFrame for CSV output.
    
    - Replace None/NaN with empty strings
    - Remove completely blank rows
    """
    if df is None or len(df) == 0:
        return pd.DataFrame()
    
    df = df.replace({None: "", np.nan: "", "None": "", "none": "", "NULL": "", "null": ""})
    
    # Remove blank rows
    is_blank = df.astype(str).apply(lambda s: s.str.strip()).eq("").all(axis=1)
    return df.loc[~is_blank].copy()


def write_df_twins(df: pd.DataFrame, base_name: str, columns: List[str], 
                   output_dir: str, run_tag: str, encoding: str = "utf-8-sig"):
    """
    Write DataFrame to two CSV files: main and timestamped.
    
    Args:
        df: DataFrame to write
        base_name: Base filename (e.g., "stage1_outcomes.csv")
        columns: Column order
        output_dir: Output directory
        run_tag: Timestamp tag for versioned copy
        encoding: Output encoding
    """
    # Ensure all columns exist
    for c in columns:
        if c not in df.columns:
            df[c] = ""
    
    df = df[columns]
    df = normalize_for_csv(df)
    
    # Write main file
    path_main = os.path.join(output_dir, base_name)
    df.to_csv(path_main, index=False, encoding=encoding)
    
    # Write timestamped copy
    name_stem = os.path.splitext(base_name)[0]
    path_tag = os.path.join(output_dir, f"{name_stem}_{run_tag}.csv")
    df.to_csv(path_tag, index=False, encoding=encoding)
    
    logging.info(f"[Output] {path_main} and {path_tag}")


# ============================================================================
# Diagnostic Logging
# ============================================================================

def init_diag_calls_csv(logs_dir: str, run_tag: str, encoding: str = "utf-8-sig") -> str:
    """
    Initialize diagnostic CSV for per-call logging.
    
    Returns:
        Path to diagnostic CSV file
    """
    diag_path = os.path.join(logs_dir, f"stage_calls_{run_tag}.csv")
    
    if not os.path.exists(diag_path):
        os.makedirs(logs_dir, exist_ok=True)
        with open(diag_path, "w", newline="", encoding=encoding) as f:
            csv.writer(f).writerow([
                "time", "stage", "study", "anchor_id", "anchor_type", 
                "prompt_chars", "response_chars",
                "http_status", "success", "attempts", "error_excerpt"
            ])
    
    return diag_path


def diag_log_call(
    diag_csv: str,
    stage: str, 
    study: str, 
    anchor_id: str, 
    anchor_type: str,
    prompt_chars: int, 
    response_chars: int,
    http_status: Optional[int], 
    success: bool, 
    attempts: int, 
    error_excerpt: str,
    encoding: str = "utf-8-sig"
):
    """Log a single LLM call to diagnostic CSV."""
    with open(diag_csv, "a", newline="", encoding=encoding) as f:
        csv.writer(f).writerow([
            datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
            stage, study, anchor_id, anchor_type, 
            prompt_chars, response_chars,
            http_status if http_status is not None else "",
            "1" if success else "0", 
            attempts, 
            (error_excerpt or "")[:300]
        ])


# ============================================================================
# Configuration Loading
# ============================================================================

def load_config(config_path: str) -> Dict[str, Any]:
    """
    Load configuration from YAML file.
    
    Args:
        config_path: Path to config.yaml
    
    Returns:
        Configuration dictionary
    """
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return config
    except ImportError:
        logging.error("PyYAML not installed. Install with: pip install pyyaml")
        raise
    except FileNotFoundError:
        logging.error(f"Config file not found: {config_path}")
        raise
    except Exception as e:
        logging.error(f"Failed to load config: {e}")
        raise


def setup_logging(config: Dict[str, Any], logs_dir: str, run_tag: str):
    """
    Setup logging based on configuration.
    
    Args:
        config: Configuration dictionary
        logs_dir: Logs directory
        run_tag: Run timestamp tag
    """
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper())
    fmt = log_config.get("format", "%(asctime)s [%(levelname)s] %(message)s")
    
    # Clear existing handlers
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    
    # File handler
    if log_config.get("file_output", True):
        os.makedirs(logs_dir, exist_ok=True)
        file_handler = logging.FileHandler(
            os.path.join(logs_dir, f"run_{run_tag}.log"),
            encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(fmt))
        logging.root.addHandler(file_handler)
    
    # Console handler
    if log_config.get("console_output", True):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter(fmt))
        logging.root.addHandler(console_handler)
    
    logging.root.setLevel(level)


# ============================================================================
# Preflight Report
# ============================================================================

def preflight_report(in_dir: str, logs_dir: str, run_tag: str, 
                     encoding: str = "utf-8-sig") -> pd.DataFrame:
    """
    Generate preflight report of input files.
    
    Args:
        in_dir: Input directory
        logs_dir: Logs directory
        run_tag: Run timestamp tag
        encoding: Output encoding
    
    Returns:
        DataFrame with file metadata
    """
    files = scan_files(in_dir)
    rows = []
    
    for p in files:
        st = os.stat(p)
        rows.append({
            "abs_path": os.path.abspath(p),
            "size_kb": round(st.st_size / 1024, 2),
            "md5": md5_of_file(p),
            "mtime": datetime.fromtimestamp(
                st.st_mtime, 
                tz=ZoneInfo("Asia/Tokyo")
            ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        })
    
    df = pd.DataFrame(rows, columns=["abs_path", "size_kb", "md5", "mtime"])
    
    print("\n=== Preflight Check: Input Files ===")
    if len(df) == 0:
        print("No .md/.markdown/.txt/.html files found.")
    else:
        print(df.to_string(index=False))
    
    # Save to logs
    os.makedirs(logs_dir, exist_ok=True)
    df.to_csv(
        os.path.join(logs_dir, f"preflight_{run_tag}.csv"), 
        index=False, 
        encoding=encoding
    )
    
    return df


if __name__ == "__main__":
    print("core.py: Utility module loaded successfully")
    print(f"Canonical outcomes: {OUTCOME_CANON_SET}")