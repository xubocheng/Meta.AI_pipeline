"""
Main extraction pipeline with all stages.

Stages:
- Stage 0: Metadata extraction
- Stage 1: Data extraction (table-first + text supplement)
- Stage 1.5: Cleaning and deduplication
- Stage 2: Validation and gap filling
"""

from __future__ import annotations

import os
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional, Tuple
from collections import Counter
import concurrent.futures as futures

import pandas as pd
import numpy as np

from core import (
    # Classes
    DeepSeekClient, RateLimiter, Anchor,
    # Functions
    load_config, setup_logging, preflight_report,
    scan_files, read_text, guess_study_id, doc_hash_text,
    find_anchors, extract_json_objects, normalize_outcome_name,
    active_anchor_keywords, expand_context, slice_by_chars,
    has_numeric_signal, is_valid_numeric_value,
    write_df_twins, init_diag_calls_csv, diag_log_call,
    # Constants
    OUTCOME_CANON_SET, OUTCOME_SYNONYMS
)


# ============================================================================
# Prompt Templates
# ============================================================================

SYSTEM_NO_CONVERSION = """You are an extraction model for animal-science RCT papers.
Return STRICT JSON only (UTF-8). Use ONLY the allowed keys per schema.
Do NOT create new keys. Do NOT infer, compute, or convert values.
Collect both FCR and G/F independently if present. Do NOT convert between them.
Do NOT compute SD from SE/SEM/CI/variance/range. Only fill SD if explicitly labeled as SD.
Do NOT convert units. If a value is not explicitly present, leave it empty."""


def prompt_stage0_metadata(study: str, text_clip: str) -> str:
    """Stage 0: Metadata extraction prompt."""
    return f"""任务：从全文抽取研究元信息，仅返回一个 JSON 对象，字段：
{{
  "Country":"...", "Authors":"...", "Location":"...", "N_total":"...",
  "pig_breeds":["..."], "Growth_stage":"...",
  "Initial_body_weight":"...", "final_body_weight":"...",
  "Treatment_doses":"按组概览描述；多组用分号",
  "Control_group":"...", "Treatment_groups":["...","..."],
  "Outcomes_list_raw":["...","..."]
}}
仅抽取文本中明确出现的信息，未知留空或空数组。仅返回 JSON。

Study: {study}
原文片段（截断）:
{text_clip}"""


def prompt_stage0_filter_outcomes(study: str, candidates: List[str]) -> str:
    """Stage 0: Outcome filtering prompt."""
    joined = "; ".join(candidates or [])
    return f"""任务：从候选指标名中，严格筛选出与 {{ADG, ADFI, FCR, G/F}} 等义的项。
仅返回 JSON：{{"filtered_keys":["ADG","ADFI","FCR","G/F"], "rationales":[{{"raw":"...","key":"..."}}]}}
不得创造新键名，不得返回解释文字。

Study: {study}
候选：{joined}"""


def prompt_docwide_samples_and_sd_alts(study: str, text_clip: str) -> str:
    """Document-wide sample size and statistical alternatives extraction."""
    return f"""任务：从全文中抽取"样本量设计规则"与"统计替代量"，分步骤提取，返回 JSON。

=== 步骤1：识别实验单位（n_unit）===
规则：
- 如果文本提到 'pens/replicates/cages'，n_unit='pen'
- 如果仅提到 'pigs/animals/heads'，n_unit='pig'
- 如果同时提到（如 '6 pens, 8 pigs per pen'），n_unit='pen'（选择较小的单位）
- 证据：记录支持判断的原文片段到 n_unit_evidence

=== 步骤2：提取每组样本量 ===
规则：
- 必须明确区分 Control 组和所有 Treatment 组
- 组名必须与原文一致（如 'Basal diet', 'Basal + 0.5% additive'）
- n 值应为实验单位的数量（pens 或 pigs，取决于 n_unit）
- 如果全文未明确各组样本量，samples_by_group 留空

=== 步骤3：提取计算参数（仅当明确时）===
如果文本提到类似 '每组 6 个重复，每个重复 8 头猪'：
- replicates_per_group: 填写重复单元数量（如 '6'）
- pigs_per_replicate: 填写每个重复的猪数（如 '8'）
- is_computed: true
- compute_note: 记录计算逻辑（如 '6 pens × 8 pigs/pen = 48 pigs total'）
注意：不要混淆 '每组 6 个重复' 和 '每组 48 头猪'

=== 步骤4：统计替代量（不计算 SD）===
仅记录原文中明确出现的 SE/SEM/CI/Variance/IQR/Range，禁止从这些值计算 SD。

=== JSON 格式（严格遵守）===
{{
  "n_unit_preferred": "pen|pig",
  "n_unit_evidence": "原文证据片段（含关键词 pens/pigs）",
  "applies_global": true|false,
  "global_n_by_group": [
    {{"group": "所有组", "n": "6"}}
  ],
  "samples_by_group": [
    {{"group": "Control", "n": "6", "evidence_locator": "Table 1"}},
    {{"group": "Treatment 1", "n": "6", "evidence_locator": "Table 1"}}
  ],
  "derivations": {{
    "replicates_per_group": "6",
    "pigs_per_replicate": "8",
    "is_computed": true|false,
    "compute_note": "6 pens × 8 pigs = 48 total"
  }},
  "stats_alternatives": [
    {{"group": "Control", "side": "C", "outcome_key": "ADG",
     "se": "", "sem": "", "ci_low": "", "ci_high": "", "ci_level": "",
     "var": "", "iqr": "", "range_low": "", "range_high": "",
     "evidence_locator": "", "note": ""}}
  ],
  "evidence_locator": "Materials and Methods, page 3"
}}

Study: {study}
全文截断：
{text_clip}"""


def prompt_stage1_outcomes(study: str, anchor: Anchor, text_clip: str) -> str:
    """Stage 1: Outcome extraction from single anchor."""
    return f"""任务：从片段中抽取 ADG/ADFI/FCR/G/F 的 Control vs Treatment 成对结果，返回 JSON 数组。

=== 抽取规则 ===
1. Outcome_key: 仅限 ADG|ADFI|FCR|G/F
2. Csample/Tsample（重要）: 
   - 优先提取 'pens/replicates/cages' 的数量
   - 如果同时提到 '6 pens, 48 pigs'，填 Csample='6', n_unit='pen'
   - 如果仅提到总猪数，填 Tsample='48', n_unit='pig'
   - 在 n_evidence_locator 中记录证据位置
3. Csd/Tsd: 仅当文本明确标注 'SD' 时填写
4. SE/SEM/CI: 填入对应字段，不做转换

=== JSON 格式（固定键名）===
[
  {{
    "Outcome_key": "ADG",
    "Outcome_raw": "ADG, g/d",
    "unit_raw": "g/d",
    "Timepoint": "",
    "Control_group": "Basal diet",
    "Treatment": "Basal + additive",
    "Add_amount": "0.5%",
    "Csample": "6",
    "Cmean": "450",
    "Csd": "15",
    "Tsample": "6",
    "Tmean": "478",
    "Tsd": "18",
    "n_unit": "pen",
    "n_is_computed": false,
    "n_compute_note": "",
    "Cse": "", "Tse": "", "Csem": "", "Tsem": "",
    "Cci_low": "", "Cci_high": "", "Tci_low": "", "Tci_high": "", "ci_level": "",
    "Cvar": "", "Tvar": "", "Ciqr": "", "Tiqr": "",
    "Crange_low": "", "Crange_high": "", "Trange_low": "", "Trange_high": "",
    "evidence_locator": "原文位置描述",
    "n_evidence_locator": "样本量证据位置",
    "sd_evidence_locator": "SD证据位置"
  }}
]

约束：只允许上述键；不得创造新键。FCR 与 G/F 若同时出现，分别保留。
禁止从 SE/SEM/CI 计算 SD。缺失留空。仅返回 JSON 数组。

Study: {study} | Anchor: {anchor.anchor_id} | Mode: {anchor.source_mode}
片段（扩窗+截断）：
{text_clip}"""


def prompt_stage1_table_exhaustive(study: str, table_block: str, ctx_text: str) -> str:
    """Stage 1: Exhaustive table extraction."""
    return f"""任务：对整张表格进行一次性抽取，返回 JSON 数组。

=== 步骤1：识别表格结构 ===
- 明确表格的列名（如 'Item', 'Control', 'T1', 'T2', 'SEM', 'P-value'）
- 识别哪些列是 Control，哪些是 Treatment
- 如果列名是缩写（如 'T1'），从表注/标题中查找完整名称

=== 步骤2：逐行抽取数据 ===
规则：
- Outcome_key: 仅限 ADG|ADFI|FCR|G/F（严格匹配）
- Control_group/Treatment: 使用表格列名（如 'Control', 'T1'）
- Csample/Tsample（重要）: 
  * 优先从表格脚注提取（如 'n=6 pens per treatment'）
  * 如果脚注提到 'pens' 和 'pigs'，填写 pens 数量，n_unit='pen'
  * 如果表格未提供，留空
  * 在 n_evidence_locator 中记录脚注位置
- Csd/Tsd: 仅当列名明确为 'SD' 时填写，'SEM'/'SE' 填入对应字段
- Timepoint: 如果表格有分阶段（如 'd 1-14', 'd 15-28'），每个阶段单独成行

=== 步骤3：穷举所有组合 ===
- 每个 Outcome × 每个 Timepoint × Control vs 每个 Treatment = 1 行
- 如果某组合无数据，不输出该行

=== JSON 格式（固定键名）===
[
  {{
    "Outcome_key": "ADG",
    "Outcome_raw": "ADG, g/d",
    "unit_raw": "g/d",
    "Timepoint": "d 1-14",
    "Control_group": "Control",
    "Treatment": "T1",
    "Add_amount": "0.5%",
    "Csample": "6",
    "Cmean": "450.2",
    "Csd": "15.3",
    "Tsample": "6",
    "Tmean": "478.5",
    "Tsd": "18.2",
    "n_unit": "pen",
    "n_is_computed": false,
    "n_compute_note": "",
    "Cse": "", "Tse": "", "Csem": "", "Tsem": "",
    "Cci_low": "", "Cci_high": "", "Tci_low": "", "Tci_high": "", "ci_level": "",
    "Cvar": "", "Tvar": "", "Ciqr": "", "Tiqr": "",
    "Crange_low": "", "Crange_high": "", "Trange_low": "", "Trange_high": "",
    "evidence_locator": "Table 2, d 1-14, Control vs T1",
    "n_evidence_locator": "Table 2 footnote",
    "sd_evidence_locator": "Table 2 column SD"
  }}
]

约束：
- 只允许上述键名，不得创造新键
- FCR 与 G/F 若同时存在，分别保留，不得互相转化
- 禁止从 SE/SEM 计算 SD
- 每个 timepoint/阶段 单独成行

Study: {study}

表格原文：
{table_block}

表注/标题上下文：
{ctx_text}"""


def prompt_stage2_fill_missing(study: str, needs: Dict[str, Any], text_clip: str) -> str:
    """Stage 2: Gap filling for missing fields."""
    keys_missing = ", ".join(sorted(k for k, v in needs.items() if (v is None or str(v).strip() == "")))
    
    return f"""任务：仅补齐指定缺失字段，返回单一 JSON；允许的键：
["Csample","Tsample","n_unit","n_is_computed","n_compute_note",
 "Cse","Tse","Csem","Tsem","Cci_low","Cci_high","Tci_low","Tci_high","ci_level",
 "Cvar","Tvar","Ciqr","Tiqr","Crange_low","Crange_high","Trange_low","Trange_high",
 "n_evidence_locator","sd_evidence_locator","Add_amount"]

规则：
- Csample/Tsample: 优先提取 pens/replicates 的数量
- 禁止填写或计算 SD（Csd/Tsd），除非片段中明确出现 SD 一词
- FCR 与 G/F 禁止互相转化
- 仅返回 JSON

Study: {study}
缺失字段: {keys_missing}
片段（扩大上下文，截断）：
{text_clip}"""


# ============================================================================
# Main Pipeline Class
# ============================================================================

class MetaExtractionPipeline:
    """Main extraction pipeline orchestrator."""
    
    def __init__(self, config_path: str):
        """
        Initialize pipeline with configuration.
        
        Args:
            config_path: Path to config.yaml
        """
        self.config = load_config(config_path)
        self.run_tag = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d_%H%M")
        
        # Setup paths
        self.in_dir = self.config["paths"]["input_dir"]
        self.out_dir = self.config["paths"]["output_dir"]
        self.logs_dir = self.config["paths"]["logs_dir"]
        os.makedirs(self.out_dir, exist_ok=True)
        os.makedirs(self.logs_dir, exist_ok=True)
        
        # Setup logging
        setup_logging(self.config, self.logs_dir, self.run_tag)
        
        # Initialize LLM clients
        self.rate_limiter = RateLimiter(
            self.config["concurrency"]["llm_rate_cap_per_min"]
        )
        
        llm_config = self.config["llm"]
        self.llm_stage0 = DeepSeekClient(
            api_key=llm_config["api_key"],
            base_url=llm_config["base_url"],
            api_path=llm_config["api_path"],
            model=llm_config["model"],
            temperature=llm_config["temperature"],
            timeout_s=llm_config["timeout_s"],
            max_tokens=llm_config["max_tokens"],
            rate_limiter=self.rate_limiter,
            usage_log_csv=os.path.join(self.logs_dir, f"usage_stage0_{self.run_tag}.csv"),
        )
        
        self.llm_stage1 = DeepSeekClient(
            api_key=llm_config["api_key"],
            base_url=llm_config["base_url"],
            api_path=llm_config["api_path"],
            model=llm_config["model"],
            temperature=llm_config["temperature"],
            timeout_s=llm_config["timeout_s"],
            max_tokens=llm_config["max_tokens"],
            rate_limiter=self.rate_limiter,
            usage_log_csv=os.path.join(self.logs_dir, f"usage_stage1_{self.run_tag}.csv"),
        )
        
        self.llm_stage2 = DeepSeekClient(
            api_key=llm_config["api_key"],
            base_url=llm_config["base_url"],
            api_path=llm_config["api_path"],
            model=llm_config["model"],
            temperature=llm_config["temperature"],
            timeout_s=llm_config["timeout_s"],
            max_tokens=llm_config["max_tokens"],
            rate_limiter=self.rate_limiter,
            usage_log_csv=os.path.join(usage_log_csv=os.path.join(self.logs_dir, f"usage_stage2_{self.run_tag}.csv")),
        )
        
        # Initialize diagnostic logging
        self.diag_csv = init_diag_calls_csv(self.logs_dir, self.run_tag)
        
        # Cache for document-wide data
        self.docwide_cache: Dict[str, Dict[str, Any]] = {}
        self.study_to_path: Dict[str, str] = {}
        
        # Column definitions
        self.overview_cols = self.config["output"]["overview_cols"]
        self.anchors_cols = [
            "Study", "doc_hash", "anchor_id", "anchor_title", "source_mode",
            "evidence_locator", "preview", "char_span_start", "char_span_end"
        ]
        self.stage1_cols = [
            "Study", "Outcome_key", "Outcome_raw", "unit_raw", "Timepoint",
            "Control_group", "Treatment", "Add_amount",
            "Csample", "Cmean", "Csd", "Tsample", "Tmean", "Tsd",
            "n_unit", "n_is_computed", "n_compute_note",
            "Cse", "Tse", "Csem", "Tsem", "Cci_low", "Cci_high", "Tci_low", "Tci_high", "ci_level",
            "Cvar", "Tvar", "Ciqr", "Tiqr", "Crange_low", "Crange_high", "Trange_low", "Trange_high",
            "sd_source",
            "evidence_locator", "n_evidence_locator", "sd_evidence_locator",
            "anchor_id", "source_mode", "doc_hash"
        ]
        
        logging.info(f"Pipeline initialized: {self.run_tag}")
        logging.info(f"Input: {self.in_dir}")
        logging.info(f"Output: {self.out_dir}")
    
    # ========================================================================
    # Stage 0: Metadata Extraction
    # ========================================================================
    
    def _process_file_stage0(self, path: str) -> Dict[str, Any]:
        """Process single file for Stage 0."""
        study = guess_study_id(path)
        text_full = read_text(path)
        
        extraction_config = self.config["extraction"]
        meta_clip = slice_by_chars(text_full, extraction_config["chars_limit_metadata"])
        
        logging.info(f"[Stage0] Processing: {study}")
        
        # (a) Metadata extraction
        raw, meta0 = self.llm_stage0.chat2(
            system_prompt=SYSTEM_NO_CONVERSION,
            user_prompt=prompt_stage0_metadata(study, meta_clip),
            stage="stage0_meta", study=study, anchor_id=""
        )
        
        diag_log_call(
            self.diag_csv, "stage0_meta", study, "", "docwide",
            meta0["prompt_len"], meta0["response_len"], meta0["status_code"],
            meta0["success"], meta0["attempts"], meta0["error"]
        )
        
        objs = extract_json_objects(raw)
        meta = objs[0] if objs else {}
        
        outcomes_raw_list = [
            str(x).strip() for x in (meta.get("Outcomes_list_raw", []) or []) 
            if str(x).strip()
        ]
        pig_breeds = ";".join(meta.get("pig_breeds", []) or [])
        treat_groups = ";".join(meta.get("Treatment_groups", []) or [])
        
        # (b) Outcome filtering
        filtered_keys: List[str] = []
        rawtoex = ""
        
        if outcomes_raw_list:
            raw2, meta1 = self.llm_stage0.chat2(
                system_prompt=SYSTEM_NO_CONVERSION,
                user_prompt=prompt_stage0_filter_outcomes(study, outcomes_raw_list),
                stage="stage0_filter_outcomes", study=study, anchor_id=""
            )
            
            diag_log_call(
                self.diag_csv, "stage0_filter_outcomes", study, "", "docwide",
                meta1["prompt_len"], meta1["response_len"], meta1["status_code"],
                meta1["success"], meta1["attempts"], meta1["error"]
            )
            
            fobjs = extract_json_objects(raw2)
            if fobjs:
                fk = fobjs[0].get("filtered_keys", [])
                filtered_keys = [k for k in fk if k in OUTCOME_CANON_SET]
                
                rats = fobjs[0].get("rationales", []) or []
                pairs = []
                for r in rats:
                    rawn = str(r.get("raw", "")).strip()
                    keyn = str(r.get("key", "")).strip()
                    if rawn and keyn in OUTCOME_CANON_SET:
                        pairs.append(f"{rawn}=>{keyn}")
                rawtoex = ";".join(pairs)
        
        # (c) Document-wide sample size extraction
        text_docwide = slice_by_chars(text_full, extraction_config["docwide_char_limit"])
        
        docwide, meta2 = self.llm_stage1.chat2(
            system_prompt=SYSTEM_NO_CONVERSION,
            user_prompt=prompt_docwide_samples_and_sd_alts(study, text_docwide),
            stage="stage1_docwide_prefetch", study=study, anchor_id=""
        )
        
        diag_log_call(
            self.diag_csv, "stage1_docwide_prefetch", study, "", "docwide",
            meta2["prompt_len"], meta2["response_len"], meta2["status_code"],
            meta2["success"], meta2["attempts"], meta2["error"]
        )
        
        dobjs = extract_json_objects(docwide)
        dwide = dobjs[0] if dobjs else {}
        
        # Default n_unit
        if not dwide.get("n_unit_preferred"):
            dwide["n_unit_preferred"] = "pen"
        
        # Smart n_unit inference
        n_unit_pref = self._infer_n_unit_smart(dwide, study)
        
        # Cache docwide data
        self.docwide_cache[study] = dwide
        
        # Extract derivations
        deriv = dwide.get("derivations", {}) or {}
        rep_per_grp = str(deriv.get("replicates_per_group", "") or "")
        pigs_per_rep = str(deriv.get("pigs_per_replicate", "") or "")
        n_is_comp = "true" if str(deriv.get("is_computed", "")).lower() == "true" else ""
        comp_note = str(deriv.get("compute_note", "") or "")
        
        # (d) Find anchors
        outcomes_config = self.config["outcomes"]
        kw = active_anchor_keywords(
            filtered_keys, 
            outcomes_config["default_keywords"],
            outcomes_config["synonyms"]
        )
        
        anchors = find_anchors(
            text_full, 
            max_anchors=extraction_config["max_anchors_per_doc"],
            keywords=kw,
            enable_overlap_suppression=extraction_config["enable_overlap_suppression"],
            overlap_threshold=extraction_config["overlap_threshold"]
        )
        
        # Prepare anchor rows
        anchor_rows = []
        for anc in anchors:
            anc.study = study
            anchor_rows.append({
                "Study": study,
                "doc_hash": anc.doc_hash,
                "anchor_id": anc.anchor_id,
                "anchor_title": anc.anchor_title,
                "source_mode": anc.source_mode,
                "evidence_locator": anc.evidence_locator,
                "preview": anc.preview,
                "char_span_start": anc.char_span[0],
                "char_span_end": anc.char_span[1],
            })
        
        # Prepare overview row
        overview_row = {
            "Study": study,
            "Country": meta.get("Country", ""),
            "Authors": meta.get("Authors", ""),
            "Location": meta.get("Location", ""),
            "N_total": meta.get("N_total", ""),
            "pig_breeds": pig_breeds,
            "Growth_stage": meta.get("Growth_stage", ""),
            "Initial_body_weight": meta.get("Initial_body_weight", ""),
            "final_body_weight": meta.get("final_body_weight", ""),
            "Treatment_doses": meta.get("Treatment_doses", ""),
            "Control_group": meta.get("Control_group", ""),
            "Treatment_groups": treat_groups,
            "replicates_per_group": rep_per_grp,
            "pigs_per_replicate": pigs_per_rep,
            "n_unit_preferred": n_unit_pref,
            "n_is_computed": n_is_comp,
            "compute_note": comp_note,
            "Outcomes_list_raw": ";".join(outcomes_raw_list),
            "Outcomes_list_rawtoex": rawtoex,
            "Outcomes_list_extract": ";".join(filtered_keys),
            "doc_hash": doc_hash_text(text_full),
        }
        
        logging.info(
            f"[Stage0] Completed: {study} | Anchors: {len(anchors)} | "
            f"Outcomes: {len(filtered_keys)} | n_unit: {n_unit_pref}"
        )
        
        return {
            "overview": overview_row,
            "anchors": anchor_rows,
            "docwide": dwide,
            "text_full": text_full
        }
    
    def stage0_metadata(self, files: Optional[List[str]] = None) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Execute Stage 0: Metadata extraction.
        
        Args:
            files: List of file paths (if None, scans input directory)
        
        Returns:
            Tuple[overview_df, anchors_df]
        """
        print("\n" + "="*80)
        print("=== Stage 0: Metadata Extraction ===")
        print("="*80)
        
        # Preflight check
        preflight_report(self.in_dir, self.logs_dir, self.run_tag)
        
        if files is None:
            files = scan_files(self.in_dir)
        
        if not files:
            logging.warning("No input files found")
            return pd.DataFrame(), pd.DataFrame()
        
        # Build study-to-path mapping
        for p in files:
            self.study_to_path[guess_study_id(p)] = p
        
        # Process files
        overview_rows = []
        anchors_rows = []
        
        max_workers = self.config["concurrency"]["max_workers_files"]
        
        with futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(self._process_file_stage0, files))
        
        for result in results:
            overview_rows.append(result["overview"])
            anchors_rows.extend(result["anchors"])
        
        # Create DataFrames
        df_overview = pd.DataFrame(overview_rows)
        df_anchors = pd.DataFrame(anchors_rows)
        
        # Write output
        write_df_twins(
            df_overview, "stage0_overview.csv", self.overview_cols,
            self.out_dir, self.run_tag
        )
        write_df_twins(
            df_anchors, "stage0_anchors.csv", self.anchors_cols,
            self.out_dir, self.run_tag
        )
        
        print(f"\n[Stage0 完成] 概览: {len(df_overview)} 篇 | 锚点: {len(df_anchors)} 个")
        
        return df_overview, df_anchors
    
    # ========================================================================
    # Stage 1: Data Extraction
    # ========================================================================
    
    def _process_anchor_stage1(
        self, 
        anchor_row: pd.Series, 
        path: str,
        processed_tables: set,
        processed_text_anchors: set
    ) -> List[Dict[str, Any]]:
        """Process single anchor for Stage 1."""
        study = anchor_row["Study"]
        anchor_id = anchor_row["anchor_id"]
        source_mode = anchor_row["source_mode"]
        start = int(anchor_row["char_span_start"]) if not pd.isna(anchor_row["char_span_start"]) else 0
        end = int(anchor_row["char_span_end"]) if not pd.isna(anchor_row["char_span_end"]) else 0
        
        text_full = read_text(path)
        dwide = self.docwide_cache.get(study, {}) or {}
        
        extraction_config = self.config["extraction"]
        rows = []
        
        # Table branch: exhaustive extraction
        if str(source_mode).lower().startswith("table"):
            table_key = (study, anchor_row["doc_hash"], start, end)
            if table_key in processed_tables:
                return rows
            processed_tables.add(table_key)
            
            table_block = text_full[start:end] if end > start else ""
            ctx = expand_context(
                text_full, start, end, 
                pad=extraction_config["pad_around_anchor"],
                limit_chars=extraction_config["chars_limit_anchor"]
            )
            
            if not has_numeric_signal(table_block + "\n" + ctx):
                logging.info(f"[Stage1] Skip table (no numeric signal): {study} | {anchor_id}")
                return rows
            
            resp, meta = self.llm_stage1.chat2(
                system_prompt=SYSTEM_NO_CONVERSION,
                user_prompt=prompt_stage1_table_exhaustive(study, table_block, ctx),
                stage="stage1_outcomes_table", study=study, anchor_id=anchor_id
            )
            
            diag_log_call(
                self.diag_csv, "stage1_outcomes_table", study, anchor_id, "table",
                meta["prompt_len"], meta["response_len"], meta["status_code"],
                meta["success"], meta["attempts"], meta["error"]
            )
            
            objs = extract_json_objects(resp)
            
            for obj in objs:
                outcome_key = normalize_outcome_name(str(obj.get("Outcome_key", "")).strip())
                if outcome_key not in OUTCOME_CANON_SET:
                    continue
                
                row = self._build_stage1_row(obj, study, anchor_id, "table", anchor_row["doc_hash"])
                
                # Apply docwide sample backfilling
                if extraction_config["docwide_eager_apply_samples"]:
                    row = self._try_fill_n_from_docwide_enhanced(row, study, dwide)
                
                rows.append(row)
            
            logging.info(f"[Stage1] Table extracted: {study} | {anchor_id} | Rows: {len(rows)}")
        
        # Text branch
        else:
            clip = expand_context(
                text_full, start, end,
                pad=extraction_config["pad_around_anchor"],
                limit_chars=extraction_config["chars_limit_anchor"]
            )
            
            if not has_numeric_signal(clip):
                logging.info(f"[Stage1] Skip text (no numeric signal): {study} | {anchor_id}")
                return rows
            
            resp, meta = self.llm_stage1.chat2(
                system_prompt=SYSTEM_NO_CONVERSION,
                user_prompt=prompt_stage1_outcomes(
                    study,
                    Anchor(
                        study=study, 
                        doc_hash=anchor_row["doc_hash"], 
                        anchor_id=anchor_id,
                        anchor_title=anchor_row.get("anchor_title", ""), 
                        source_mode=source_mode,
                        evidence_locator=anchor_row.get("evidence_locator", ""),
                        preview=anchor_row.get("preview", ""), 
                        char_span=(start, end)
                    ),
                    clip
                ),
                stage="stage1_outcomes_text", study=study, anchor_id=anchor_id
            )
            
            diag_log_call(
                self.diag_csv, "stage1_outcomes_text", study, anchor_id, "text",
                meta["prompt_len"], meta["response_len"], meta["status_code"],
                meta["success"], meta["attempts"], meta["error"]
            )
            
            processed_text_anchors.add((study, anchor_id))
            objs = extract_json_objects(resp)
            
            for obj in objs:
                outcome_key = normalize_outcome_name(str(obj.get("Outcome_key", "")).strip())
                if outcome_key not in OUTCOME_CANON_SET:
                    continue
                
                row = self._build_stage1_row(obj, study, anchor_id, "text", anchor_row["doc_hash"])
                
                # Apply docwide sample backfilling
                if extraction_config["docwide_eager_apply_samples"]:
                    row = self._try_fill_n_from_docwide_enhanced(row, study, dwide)
                
                rows.append(row)
            
            logging.info(f"[Stage1] Text extracted: {study} | {anchor_id} | Rows: {len(rows)}")
        
        return rows
    
    def _build_stage1_row(
        self, 
        obj: Dict[str, Any], 
        study: str, 
        anchor_id: str, 
        source_mode: str, 
        doc_hash: str
    ) -> Dict[str, Any]:
        """Build Stage 1 row from extracted object."""
        row = {
            "Study": study,
            "Outcome_key": obj.get("Outcome_key", ""),
            "Outcome_raw": obj.get("Outcome_raw", ""),
            "unit_raw": obj.get("unit_raw", ""),
            "Timepoint": obj.get("Timepoint", ""),
            "Control_group": obj.get("Control_group", ""),
            "Treatment": obj.get("Treatment", ""),
            "Add_amount": obj.get("Add_amount", ""),
            "Csample": obj.get("Csample", ""),
            "Cmean": obj.get("Cmean", ""),
            "Csd": obj.get("Csd", ""),
            "Tsample": obj.get("Tsample", ""),
            "Tmean": obj.get("Tmean", ""),
            "Tsd": obj.get("Tsd", ""),
            "n_unit": obj.get("n_unit", ""),
            "n_is_computed": "true" if str(obj.get("n_is_computed", "")).lower() == "true" else "",
            "n_compute_note": obj.get("n_compute_note", ""),
            "Cse": obj.get("Cse", ""), "Tse": obj.get("Tse", ""),
            "Csem": obj.get("Csem", ""), "Tsem": obj.get("Tsem", ""),
            "Cci_low": obj.get("Cci_low", ""), "Cci_high": obj.get("Cci_high", ""),
            "Tci_low": obj.get("Tci_low", ""), "Tci_high": obj.get("Tci_high", ""),
            "ci_level": obj.get("ci_level", ""),
            "Cvar": obj.get("Cvar", ""), "Tvar": obj.get("Tvar", ""),
            "Ciqr": obj.get("Ciqr", ""), "Tiqr": obj.get("Tiqr", ""),
            "Crange_low": obj.get("Crange_low", ""), "Crange_high": obj.get("Crange_high", ""),
            "Trange_low": obj.get("Trange_low", ""), "Trange_high": obj.get("Trange_high", ""),
            "evidence_locator": obj.get("evidence_locator", ""),
            "n_evidence_locator": obj.get("n_evidence_locator", ""),
            "sd_evidence_locator": obj.get("sd_evidence_locator", ""),
            "anchor_id": anchor_id,
            "source_mode": source_mode,
            "doc_hash": doc_hash,
        }
        
        # Mark SD source
        row["sd_source"] = "from_text" if (str(row["Csd"]).strip() or str(row["Tsd"]).strip()) else ""
        
        return row
    
    def stage1_extraction(self, df_anchors: pd.DataFrame) -> pd.DataFrame:
        """
        Execute Stage 1: Data extraction.
        
        Args:
            df_anchors: Anchors DataFrame from Stage 0
        
        Returns:
            Stage 1 outcomes DataFrame
        """
        print("\n" + "="*80)
        print("=== Stage 1: Data Extraction ===")
        print("="*80)
        
        if len(df_anchors) == 0:
            logging.warning("No anchors to process")
            return pd.DataFrame()
        
        stage1_rows = []
        processed_tables = set()
        processed_text_anchors = set()
        
        max_workers = self.config["concurrency"]["max_workers_files"]
        
        # Process anchors
        with futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            tasks = []
            for _, r in df_anchors.iterrows():
                st = r["Study"]
                p = self.study_to_path.get(st, "")
                if p and os.path.exists(p):
                    tasks.append(ex.submit(
                        self._process_anchor_stage1, 
                        r, p, processed_tables, processed_text_anchors
                    ))
            
            for t in futures.as_completed(tasks):
                try:
                    rows = t.result()
                    stage1_rows.extend(rows)
                except Exception as e:
                    logging.exception(f"[Stage1] Task error: {e}")
        
        # Deduplicate
        df_stage1 = pd.DataFrame(stage1_rows)
        
        if len(df_stage1) > 0:
            df_stage1 = self._deduplicate_stage1(df_stage1)
        
        # Write output
        write_df_twins(
            df_stage1, "stage1_outcomes.csv", self.stage1_cols,
            self.out_dir, self.run_tag
        )
        
        print(f"\n[Stage1 完成] 抽取行数: {len(df_stage1)}")
        print(f"  - 表格处理: {len(processed_tables)} 个")
        print(f"  - 文本锚点: {len(processed_text_anchors)} 个")
        
        return df_stage1
    
    def _deduplicate_stage1(self, df: pd.DataFrame) -> pd.DataFrame:
        """Deduplicate Stage 1 data."""
        def digits_sig(*vals):
            nums = []
            for v in vals:
                s = str(v)
                nums += re.findall(r"-?\d+(?:\.\d+)?", s)
            return "|".join(nums[:8])
        
        df["dedupe_key"] = (
            df["Study"].astype(str) + "|" + 
            df["Outcome_key"].astype(str) + "|" +
            df["Control_group"].astype(str).str.lower() + "|" +
            df["Treatment"].astype(str).str.lower() + "|" +
            df["Timepoint"].astype(str) + "|" +
            df.apply(lambda r: digits_sig(r["Cmean"], r["Csd"], r["Tmean"], r["Tsd"]), axis=1) + "|" +
            df["evidence_locator"].astype(str).str[:80]
        )
        
        before_dedup = len(df)
        df = df.drop_duplicates(subset=["dedupe_key"]).drop(columns=["dedupe_key"]).reset_index(drop=True)
        after_dedup = len(df)
        
        logging.info(f"[Stage1] Deduplication: {before_dedup} → {after_dedup} rows")
        
        return df
    
    # ========================================================================
    # Stage 1.5: Cleaning and Deduplication
    # ========================================================================
    
    def stage1_5_cleaning(self, df_stage1: pd.DataFrame) -> pd.DataFrame:
        """
        Execute Stage 1.5: Enhanced cleaning and deduplication.
        
        Args:
            df_stage1: Stage 1 outcomes DataFrame
        
        Returns:
            Cleaned DataFrame
        """
        print("\n" + "="*80)
        print("=== Stage 1.5: Cleaning and Deduplication ===")
        print("="*80)
        
        if len(df_stage1) == 0:
            logging.warning("No data to clean")
            return pd.DataFrame()
        
        # Step 1: Remove invalid means
        print("\n[步骤1] 删除无效 mean 行...")
        valid_means_mask = df_stage1.apply(self._has_valid_means, axis=1)
        df_cleaned = df_stage1[valid_means_mask].copy()
        
        dropped = len(df_stage1) - len(df_cleaned)
        print(f"  - 删除无效 mean 行: {dropped} 行")
        
        if len(df_cleaned) == 0:
            logging.warning("No data after cleaning")
            return pd.DataFrame()
        
        # Step 2: Calculate priority scores
        print("\n[步骤2] 计算去重优先级...")
        df_cleaned["completeness"] = df_cleaned.apply(self._completeness_score, axis=1)
        df_cleaned["source_priority"] = df_cleaned["source_mode"].apply(
            lambda x: 1 if str(x).lower().startswith("table") else 0
        )
        df_cleaned["computed_priority"] = df_cleaned.apply(self._compute_priority_score, axis=1)
        df_cleaned["semantic_key"] = df_cleaned.apply(self._make_semantic_key_strict, axis=1)
        
        print(f"  - 完整度评分范围: {df_cleaned['completeness'].min():.1f} ~ {df_cleaned['completeness'].max():.1f}")
        print(f"  - Table 来源: {df_cleaned['source_priority'].sum()} 行")
        
        # Step 3: Multi-level deduplication
        print("\n[步骤3] 执行多级优先级去重...")
        before_dedup = len(df_cleaned)
        
        df_sorted = df_cleaned.sort_values(
            by=["semantic_key", "completeness", "source_priority", "computed_priority", "anchor_id"],
            ascending=[True, False, False, False, True]
        )
        
        df_deduped = (
            df_sorted
            .drop_duplicates(subset=["semantic_key"], keep="first")
            .drop(columns=["semantic_key", "completeness", "source_priority", "computed_priority"])
            .sort_values(["Study", "Outcome_key", "Treatment", "Timepoint"])
            .reset_index(drop=True)
        )
        
        after_dedup = len(df_deduped)
        print(f"  - 去重: {before_dedup} → {after_dedup} 行 (删除 {before_dedup - after_dedup} 行)")
        
        # Step 4: Sample consistency enforcement
        print("\n[步骤4] 强制 Sample 一致性修正...")
        df_deduped, corrected_count = self._enforce_sample_consistency(df_deduped)
        print(f"  - 修正行数: {corrected_count} 行")
        
        # Write output
        write_df_twins(
            df_deduped, "stage1.5_cleaned.csv", self.stage1_cols,
            self.out_dir, self.run_tag
        )
        
        print(f"\n[Stage1.5 完成] 最终行数: {len(df_deduped)}")
        
        return df_deduped
    
    def _has_valid_means(self, row: pd.Series) -> bool:
        """Check if row has valid Cmean and Tmean."""
        return (is_valid_numeric_value(row.get("Cmean")) and 
                is_valid_numeric_value(row.get("Tmean")))
    
    def _completeness_score(self, row: pd.Series) -> float:
        """Calculate row completeness score."""
        score = 0.0
        
        # Core fields (weight 15)
        if is_valid_numeric_value(row.get("Cmean")):
            score += 15
        if is_valid_numeric_value(row.get("Tmean")):
            score += 15
        
        # Sample size (weight 10)
        if is_valid_numeric_value(row.get("Csample")):
            score += 10
        if is_valid_numeric_value(row.get("Tsample")):
            score += 10
        
        # SD (weight 5)
        if is_valid_numeric_value(row.get("Csd")):
            score += 5
        if is_valid_numeric_value(row.get("Tsd")):
            score += 5
        
        # SE/SEM (weight 3)
        for k in ["Cse", "Tse", "Csem", "Tsem"]:
            if is_valid_numeric_value(row.get(k)):
                score += 3
        
        # CI (weight 2)
        for k in ["Cci_low", "Cci_high", "Tci_low", "Tci_high"]:
            if is_valid_numeric_value(row.get(k)):
                score += 2
        
        # Metadata
        if str(row.get("Add_amount", "")).strip():
            score += 2
        if str(row.get("Timepoint", "")).strip():
            score += 1
        
        return score
    
    def _compute_priority_score(self, row: pd.Series) -> int:
        """
        Compute priority score for deduplication.
        
        Priority levels:
        3 = table + pen (non-computed) - Highest reliability
        2 = computed values
        1 = text + pen (non-computed)
        0 = others
        """
        source = str(row.get("source_mode", "")).lower()
        n_unit = str(row.get("n_unit", "")).lower().strip()
        is_computed = str(row.get("n_is_computed", "")).lower() == "true"
        
        if source.startswith("table") and n_unit == "pen" and not is_computed:
            return 3
        elif is_computed:
            return 2
        elif n_unit == "pen" and not is_computed:
            return 1
        else:
            return 0
    
    def _make_semantic_key_strict(self, row: pd.Series) -> str:
        """Generate strict semantic deduplication key."""
        def safe_round(v, decimals=3):
            if not is_valid_numeric_value(v):
                return "MISSING"
            try:
                num = float(v)
                return f"{num:.{decimals}f}"
            except:
                return "INVALID"
        
        return "|".join([
            str(row.get("Study", "")).strip(),
            str(row.get("Outcome_key", "")).strip(),
            str(row.get("Control_group", "")).lower().strip(),
            str(row.get("Treatment", "")).lower().strip(),
            str(row.get("Timepoint", "")).strip(),
            safe_round(row.get("Cmean")),
            safe_round(row.get("Tmean")),
            safe_round(row.get("Csample")),
            safe_round(row.get("Tsample")),
            str(row.get("n_unit", "")).lower().strip(),
            safe_round(row.get("Csd")),
            safe_round(row.get("Tsd")),
        ])
    
    def _enforce_sample_consistency(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
        """Enforce Csample = Tsample consistency."""
        df = df.copy()
        corrected_count = 0
        
        for idx in range(len(df)):
            row = df.iloc[idx]
            study = row["Study"]
            dwide = self.docwide_cache.get(study, {})
            
            csample = str(row.get("Csample", "")).strip()
            tsample = str(row.get("Tsample", "")).strip()
            
            needs_correction = False
            if not csample or not tsample:
                needs_correction = True
            elif csample != tsample:
                needs_correction = True
                logging.warning(
                    f"[{study}][row {idx}] Sample mismatch: Csample={csample}, Tsample={tsample}"
                )
            
            if needs_correction:
                derivs = dwide.get("derivations", {}) or {}
                is_comp = derivs.get("is_computed", False)
                
                if is_comp:
                    rep = str(derivs.get("replicates_per_group", "")).strip()
                    ppr = str(derivs.get("pigs_per_replicate", "")).strip()
                    n_unit = dwide.get("n_unit_preferred", "pen")
                    
                    if rep and ppr:
                        try:
                            if n_unit == "pen":
                                computed_n = rep
                            else:
                                computed_n = str(int(float(rep)) * int(float(ppr)))
                            
                            df.iloc[idx, df.columns.get_loc("Csample")] = computed_n
                            df.iloc[idx, df.columns.get_loc("Tsample")] = computed_n
                            df.iloc[idx, df.columns.get_loc("n_unit")] = n_unit
                            df.iloc[idx, df.columns.get_loc("n_is_computed")] = "true"
                            
                            comp_note = derivs.get("compute_note", "")
                            if comp_note:
                                df.iloc[idx, df.columns.get_loc("n_compute_note")] = comp_note
                            
                            corrected_count += 1
                        except Exception as e:
                            logging.warning(f"[Stage1.5] Sample correction failed for {study} row {idx}: {e}")
        
        return df, corrected_count
    
    # ========================================================================
    # Stage 2: Validation and Gap Filling
    # ========================================================================
    
    def stage2_validation(self, df_cleaned: pd.DataFrame) -> pd.DataFrame:
        """
        Execute Stage 2: Validation and gap filling.
        
        Args:
            df_cleaned: Cleaned DataFrame from Stage 1.5
        
        Returns:
            Final validated DataFrame
        """
        print("\n" + "="*80)
        print("=== Stage 2: Validation and Gap Filling ===")
        print("="*80)
        
        if len(df_cleaned) == 0:
            logging.warning("No data to validate")
            return pd.DataFrame()
        
        # Step 1: Rule-based validation
        print("\n[步骤1] 执行规则验证...")
        validation_rows = []
        
        for idx, row in df_cleaned.iterrows():
            study = row["Study"]
            path = self.study_to_path.get(study, "")
            
            if not path or not os.path.exists(path):
                validation_rows.append({
                    "Study": study,
                    "row_index": idx,
                    "is_valid": False,
                    "issues": "SOURCE_FILE_NOT_FOUND",
                    "issue_count": 1,
                    "severity": "HIGH"
                })
                continue
            
            text_full = read_text(path)
            result = self._validate_row_rules(row, text_full)
            
            validation_rows.append({
                "Study": study,
                "row_index": idx,
                "Outcome_key": row.get("Outcome_key", ""),
                "Treatment": row.get("Treatment", ""),
                "n_unit": row.get("n_unit", ""),
                "Csample": row.get("Csample", ""),
                "is_valid": result["is_valid"],
                "issues": result["issues"],
                "issue_count": result["issue_count"],
                "severity": result["severity"]
            })
        
        df_validation = pd.DataFrame(validation_rows)
        
        # Save validation log
        validation_log_path = os.path.join(self.logs_dir, f"stage2_validation_{self.run_tag}.csv")
        df_validation.to_csv(validation_log_path, index=False, encoding="utf-8-sig")
        
        # Validation statistics
        total_rows = len(df_validation)
        invalid_rows = len(df_validation[~df_validation["is_valid"]])
        high_severity = len(df_validation[df_validation["severity"] == "HIGH"])
        
        print(f"  总行数: {total_rows}")
        print(f"  无效行: {invalid_rows} ({invalid_rows/total_rows*100:.1f}%)")
        print(f"  高严重性: {high_severity}")
        
        # Step 2: Identify rows needing LLM gap filling
        print("\n[步骤2] 识别需要 LLM 补缺的行...")
        
        df_with_validation = df_cleaned.copy()
        df_with_validation["validation_severity"] = df_validation["severity"]
        df_with_validation["validation_issues"] = df_validation["issues"]
        
        needs_llm_mask = df_with_validation.apply(
            lambda row: self._needs_llm_filling(row, df_validation.loc[row.name]), 
            axis=1
        )
        
        high_risk_indices = df_with_validation[needs_llm_mask].index.tolist()
        
        # Cost control
        max_fill = self.config["validation"]["max_fillmissing_rows"]
        if len(high_risk_indices) > max_fill:
            print(f"  成本控制：限制补缺数量为 {max_fill} 行")
            severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
            df_priority = df_with_validation.loc[high_risk_indices].copy()
            df_priority["severity_rank"] = df_priority["validation_severity"].map(severity_order)
            df_priority = df_priority.sort_values("severity_rank")
            high_risk_indices = df_priority.head(max_fill).index.tolist()
        
        print(f"  需要补缺: {len(high_risk_indices)} 行")
        
        # Step 3: Targeted gap filling
        if len(high_risk_indices) > 0:
            print("\n[步骤3] 执行定向补缺...")
            patches = self._fill_missing_targeted(df_cleaned, high_risk_indices)
            print(f"  完成: {len(patches)} 行成功补缺")
            
            # Apply patches
            df_stage2 = df_cleaned.copy()
            for idx, patch in patches.items():
                for k, v in patch.items():
                    if k in df_stage2.columns and str(v).strip():
                        df_stage2.iloc[idx, df_stage2.columns.get_loc(k)] = v
        else:
            df_stage2 = df_cleaned.copy()
        
        # Step 4: Final deduplication
        print("\n[步骤4] 最终去重...")
        df_stage2 = self._deduplicate_stage1(df_stage2)
        
        # Write output
        write_df_twins(
            df_stage2, "stage2_final.csv", self.stage1_cols,
            self.out_dir, self.run_tag
        )
        
        print(f"\n[Stage2 完成] 最终行数: {len(df_stage2)}")
        
        return df_stage2
    
    def _validate_row_rules(self, row: pd.Series, text_full: str) -> Dict[str, Any]:
        """Rule-based validation (zero-cost)."""
        issues = []
        
        # Required fields
        for field in ["Cmean", "Tmean", "Control_group", "Treatment", "Outcome_key"]:
            if not str(row.get(field, "")).strip():
                issues.append(f"MISSING_{field}")
        
        # Numeric validity
        for field in ["Cmean", "Tmean"]:
            val = str(row.get(field, "")).strip()
            if val:
                try:
                    num = float(val)
                    if num < 0:
                        issues.append(f"{field}_NEGATIVE")
                    if num > 10000:
                        issues.append(f"{field}_ABNORMAL_LARGE")
                except:
                    issues.append(f"{field}_NOT_NUMERIC")
        
        # SD checks
        for prefix in ["C", "T"]:
            mean_val = str(row.get(f"{prefix}mean", "")).strip()
            sd_val = str(row.get(f"{prefix}sd", "")).strip()
            
            if mean_val and sd_val:
                try:
                    mean = float(mean_val)
                    sd = float(sd_val)
                    if sd > mean * 3:
                        issues.append(f"{prefix}SD_EXTREMELY_LARGE")
                except:
                    pass
        
        # Sample consistency
        csample = str(row.get("Csample", "")).strip()
        tsample = str(row.get("Tsample", "")).strip()
        
        if csample and tsample and csample != tsample:
            issues.append("SAMPLE_MISMATCH")
        
        # pen/pig range validation
        n_unit = str(row.get("n_unit", "")).lower().strip()
        if n_unit and csample:
            try:
                n = int(float(csample))
                if n_unit == "pen" and (n < 3 or n > 25):
                    issues.append("PEN_OUT_OF_RANGE")
                elif n_unit == "pig" and (n < 20 or n > 500):
                    issues.append("PIG_OUT_OF_RANGE")
            except:
                pass
        
        # Severity determination
        severity = "LOW"
        if any("MISSING" in i for i in issues):
            severity = "HIGH"
        elif any("NEGATIVE" in i or "EXTREMELY" in i or "MISMATCH" in i for i in issues):
            severity = "HIGH"
        elif any("OUT_OF_RANGE" in i or "ABNORMAL" in i for i in issues):
            severity = "MEDIUM"
        
        return {
            "is_valid": len(issues) == 0,
            "issues": "; ".join(issues),
            "issue_count": len(issues),
            "severity": severity
        }
    
    def _needs_llm_filling(self, row: pd.Series, validation_result: pd.Series) -> bool:
        """Determine if row needs LLM gap filling."""
        if validation_result["severity"] == "HIGH" and "SOURCE_FILE_NOT_FOUND" not in validation_result["issues"]:
            return True
        
        if not str(row.get("Csample", "")).strip() or not str(row.get("Tsample", "")).strip():
            return True
        
        if not str(row.get("Add_amount", "")).strip():
            return True
        
        return False
    
    def _fill_missing_targeted(
        self, 
        df: pd.DataFrame, 
        indices: List[int]
    ) -> Dict[int, Dict[str, Any]]:
        """Targeted LLM gap filling for high-risk rows."""
        patches = {}
        
        extraction_config = self.config["extraction"]
        
        def fill_row(idx: int) -> Tuple[int, Dict[str, Any]]:
            row = df.iloc[idx]
            study = row["Study"]
            path = self.study_to_path.get(study, "")
            
            if not path or not os.path.exists(path):
                return idx, {}
            
            text_full = read_text(path)
            anchor_id = str(row.get("anchor_id", ""))
            
            # Get context
            ctx = slice_by_chars(text_full, extraction_config["chars_limit_stage2"])
            
            # Define needs
            needs_keys = [
                "Csample", "Tsample", "n_unit", "n_is_computed", "n_compute_note",
                "Cse", "Tse", "Csem", "Tsem", "n_evidence_locator", "Add_amount"
            ]
            needs = {k: row.get(k, "") for k in needs_keys}
            
            # Call LLM
            resp, meta = self.llm_stage2.chat2(
                system_prompt=SYSTEM_NO_CONVERSION,
                user_prompt=prompt_stage2_fill_missing(study, needs, ctx),
                stage="stage2_gap_fill", study=study, anchor_id=anchor_id
            )
            
            diag_log_call(
                self.diag_csv, "stage2_gap_fill", study, anchor_id, "targeted",
                meta["prompt_len"], meta["response_len"], meta["status_code"],
                meta["success"], meta["attempts"], meta["error"]
            )
            
            if not meta["success"]:
                return idx, {}
            
            objs = extract_json_objects(resp)
            if not objs:
                return idx, {}
            
            patch = {}
            for k in needs_keys:
                v = str(objs[0].get(k, "")).strip()
                if v:
                    patch[k] = v
            
            return idx, patch
        
        max_workers = self.config["concurrency"]["max_workers_files"]
        
        with futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(fill_row, indices))
            for idx, patch in results:
                if patch:
                    patches[idx] = patch
                return patches
    
    # ========================================================================
    # Helper: Smart n_unit Inference
    # ========================================================================
    
    def _infer_n_unit_smart(self, dwide: Dict, study: str) -> str:
        """
        Smart n_unit inference.
        
        Priority:
        1. Numeric range rules
        2. Keyword matching
        3. LLM extraction
        4. Default: 'pen'
        """
        n_unit_llm = str(dwide.get("n_unit_preferred", "")).strip().lower()
        
        # Get derivation parameters
        derivs = dwide.get("derivations", {}) or {}
        rep = str(derivs.get("replicates_per_group", "")).strip()
        ppr = str(derivs.get("pigs_per_replicate", "")).strip()
        
        # Rule 1: Numeric range judgment
        if rep and ppr:
            try:
                rep_num = int(float(rep))
                ppr_num = int(float(ppr))
                
                if 3 <= rep_num <= 20 and 4 <= ppr_num <= 30:
                    total = rep_num * ppr_num
                    if 30 <= total <= 300:
                        logging.info(f"[{study}] Inferred n_unit=pen (rep={rep_num}, ppr={ppr_num}, total={total})")
                        return "pen"
                
                # Correction for abnormal rep
                if rep_num > 30 and 4 <= ppr_num <= 30:
                    actual_rep = rep_num // ppr_num
                    if 3 <= actual_rep <= 20:
                        logging.info(f"[{study}] Corrected: rep={actual_rep} (was {rep_num})")
                        return "pen"
            except:
                pass
        
        # Rule 2: Keyword matching
        evidence = str(dwide.get("evidence_locator", "")).lower()
        n_unit_evidence = str(dwide.get("n_unit_evidence", "")).lower()
        combined_text = evidence + " " + n_unit_evidence
        
        pen_keywords = ["pen", "replicate", "cage", "栏", "重复"]
        pig_keywords = ["pig", "animal", "head", "猪", "头"]
        
        pen_score = sum(1 for kw in pen_keywords if kw in combined_text)
        pig_score = sum(1 for kw in pig_keywords if kw in combined_text)
        
        if pen_score > pig_score:
            logging.info(f"[{study}] Inferred n_unit=pen (keyword score: pen={pen_score}, pig={pig_score})")
            return "pen"
        elif pig_score > pen_score:
            logging.info(f"[{study}] Inferred n_unit=pig (keyword score)")
            return "pig"
        
        # Rule 3: LLM result
        if n_unit_llm in ["pen", "pens", "replicate", "replicates"]:
            return "pen"
        elif n_unit_llm in ["pig", "pigs", "animal", "animals", "head"]:
            return "pig"
        
        # Default
        logging.warning(f"[{study}] Unable to infer n_unit, using default 'pen'")
        return "pen"
    
    # ========================================================================
    # Helper: Enhanced Sample Backfilling
    # ========================================================================
    
    def _try_fill_n_from_docwide_enhanced(self, row: Dict[str, Any], study: str, dwide: Dict) -> Dict[str, Any]:
        """
        Enhanced sample size backfilling with 5-level priority.
        
        Priority:
        1. Existing table values (no override)
        2. Exact group name matching
        3. Synonym matching
        4. Global rules
        5. Computed values (validated)
        """
        n_unit = self._infer_n_unit_smart(dwide, study)
        
        by_group = dwide.get("samples_by_group", []) or []
        global_map = dwide.get("global_n_by_group", []) or []
        applies_global = bool(dwide.get("applies_global", False))
        
        derivs = dwide.get("derivations", {}) or {}
        computed_n, is_valid, compute_note = self._validate_and_correct_computed_n(derivs, n_unit, study)
        
        for side in ["C", "T"]:
            key = "Csample" if side == "C" else "Tsample"
            
            # Priority 1: Keep existing
            existing = str(row.get(key, "")).strip()
            if existing and is_valid_numeric_value(existing):
                continue
            
            gname = row["Control_group"] if side == "C" else row["Treatment"]
            
            # Priority 2: Exact group matching
            matched_n = self._match_sample_by_group(gname, by_group, global_map if applies_global else [])
            
            if matched_n:
                row[key] = matched_n
                logging.debug(f"[{study}] Filled {key}={matched_n} via group matching")
                continue
            
            # Priority 3: Global rules
            if applies_global and global_map:
                for ent in global_map:
                    n_val = str(ent.get("n", "")).strip()
                    if n_val:
                        row[key] = n_val
                        logging.debug(f"[{study}] Filled {key}={n_val} via global rule")
                        break
                if str(row.get(key, "")).strip():
                    continue
            
            # Priority 4: Computed values
            if is_valid and computed_n:
                row[key] = computed_n
                row["n_is_computed"] = "true"
                row["n_compute_note"] = compute_note
                logging.debug(f"[{study}] Filled {key}={computed_n} via computation")
        
        # Set n_unit
        if not str(row.get("n_unit", "")).strip():
            row["n_unit"] = n_unit
        
        return row
    
    def _match_sample_by_group(self, gname: str, by_group: List[Dict], global_map: List[Dict]) -> Optional[str]:
        """Multi-level group name matching."""
        if not gname:
            return None
        
        gname_clean = gname.strip().lower()
        
        # Level 1: Exact match
        for ent in by_group:
            nm = str(ent.get("group", "")).strip().lower()
            if nm == gname_clean and str(ent.get("n", "")).strip():
                return str(ent["n"]).strip()
        
        # Level 2: Synonym match
        synonyms = {
            "control": ["control", "basal", "nc", "negative control", "con", "对照", "基础"],
            "treatment": ["treatment", "experimental", "positive", "trt", "试验", "处理"],
        }
        
        category = None
        for cat, syns in synonyms.items():
            if any(syn in gname_clean for syn in syns):
                category = cat
                break
        
        if category:
            for ent in by_group:
                nm = str(ent.get("group", "")).strip().lower()
                if any(syn in nm for syn in synonyms[category]):
                    n_val = str(ent.get("n", "")).strip()
                    if n_val:
                        logging.info(f"Matched '{gname}' → '{ent.get('group')}' via synonym")
                        return n_val
        
        # Level 3: Fuzzy match
        for ent in by_group:
            nm = str(ent.get("group", "")).strip().lower()
            if nm and len(nm) >= 3:
                if (nm in gname_clean or gname_clean in nm) and abs(len(nm) - len(gname_clean)) <= 5:
                    n_val = str(ent.get("n", "")).strip()
                    if n_val:
                        logging.warning(f"Fuzzy matched '{gname}' → '{ent.get('group')}'")
                        return n_val
        
        # Level 4: Global rules
        for ent in global_map:
            nm = str(ent.get("group", "")).strip().lower()
            if nm == gname_clean and str(ent.get("n", "")).strip():
                return str(ent["n"]).strip()
        
        return None
    
    def _validate_and_correct_computed_n(self, derivs: Dict, n_unit: str, study: str) -> Tuple[str, bool, str]:
        """Validate and correct computed sample size."""
        rep = str(derivs.get("replicates_per_group", "")).strip()
        ppr = str(derivs.get("pigs_per_replicate", "")).strip()
        
        if not rep or not ppr:
            return "", False, "Missing parameters"
        
        try:
            rep_num = int(float(rep))
            ppr_num = int(float(ppr))
            
            # Validation 1: Numeric range
            if not (3 <= rep_num <= 20):
                logging.warning(f"[{study}] Abnormal replicates={rep_num}")
                if 30 <= rep_num <= 300 and 4 <= ppr_num <= 30:
                    corrected_rep = rep_num // ppr_num
                    if 3 <= corrected_rep <= 20:
                        rep_num = corrected_rep
            
            if not (4 <= ppr_num <= 30):
                return "", False, f"Invalid pigs_per_replicate={ppr_num}"
            
            # Validation 2: Unit consistency
            if n_unit == "pen":
                computed_n = str(rep_num)
                note = f"n_pens={rep_num}, pigs_per_pen={ppr_num}"
            else:
                computed_n = str(rep_num * ppr_num)
                note = f"total_pigs={rep_num}×{ppr_num}={computed_n}"
            
            # Validation 3: Total reasonableness
            total_pigs = rep_num * ppr_num
            if not (20 <= total_pigs <= 500):
                logging.warning(f"[{study}] Abnormal total_pigs={total_pigs}")
                return computed_n, False, f"Total pigs={total_pigs} out of range"
            
            return computed_n, True, note
        
        except Exception as e:
            logging.error(f"[{study}] Failed to compute n: {e}")
            return "", False, str(e)
    
    # ========================================================================
    # Main Pipeline
    # ========================================================================
    
    def run_full_pipeline(
        self, 
        input_dir: Optional[str] = None, 
        output_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Run full extraction pipeline (Stage 0 → Stage 2).
        
        Args:
            input_dir: Override input directory
            output_dir: Override output directory
        
        Returns:
            Dictionary with pipeline results
        """
        if input_dir:
            self.in_dir = input_dir
        if output_dir:
            self.out_dir = output_dir
            os.makedirs(self.out_dir, exist_ok=True)
        
        print("\n" + "="*80)
        print("=== Meta-Extraction Pipeline: Full Run ===")
        print("="*80)
        print(f"Input: {self.in_dir}")
        print(f"Output: {self.out_dir}")
        print(f"Run tag: {self.run_tag}")
        
        # Stage 0
        df_overview, df_anchors = self.stage0_metadata()
        
        # Stage 1
        df_stage1 = self.stage1_extraction(df_anchors)
        
        # Stage 1.5
        df_cleaned = self.stage1_5_cleaning(df_stage1)
        
        # Stage 2
        df_final = self.stage2_validation(df_cleaned)
        
        # Summary
        print("\n" + "="*80)
        print("=== Pipeline Complete ===")
        print("="*80)
        print(f"Studies processed: {len(df_overview)}")
        print(f"Anchors found: {len(df_anchors)}")
        print(f"Stage 1 rows: {len(df_stage1)}")
        print(f"Cleaned rows: {len(df_cleaned)}")
        print(f"Final rows: {len(df_final)}")
        print(f"\nOutput directory: {os.path.abspath(self.out_dir)}")
        print(f"Logs directory: {os.path.abspath(self.logs_dir)}")
        
        return {
            "studies": len(df_overview),
            "anchors": len(df_anchors),
            "stage1_rows": len(df_stage1),
            "cleaned_rows": len(df_cleaned),
            "final_rows": len(df_final),
            "run_tag": self.run_tag
        }


# ============================================================================
# CLI Interface
# ============================================================================

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python extraction.py <config.yaml>")
        print("\nExample:")
        print("  python extraction.py config.yaml")
        sys.exit(1)
    
    config_path = sys.argv[1]
    
    if not os.path.exists(config_path):
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    
    # Run pipeline
    pipeline = MetaExtractionPipeline(config_path)
    results = pipeline.run_full_pipeline()
    
    print("\n✓ Pipeline completed successfully")
    print(f"✓ Extracted {results['final_rows']} data points from {results['studies']} studies")