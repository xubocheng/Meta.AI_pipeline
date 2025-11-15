# Meta-Extraction System for Animal Science Literature

A LLM-based automated data extraction system for animal experiment literature, specializing in growth performance metrics (ADG, ADFI, FCR, G/F).

## Features

- **Multi-modal extraction**: Supports Markdown tables, HTML tables, and text paragraphs
- **Smart sample size inference**: Distinguishes between pen (experimental unit) and pig (individual animal)
- **Enhanced deduplication**: Multi-level priority-based semantic deduplication
- **Rule-based validation**: Detects SD anomalies, sample size inconsistencies, and pen/pig confusion

## Installation
```bash
pip install -r requirements.txt
```

## Configuration
1. Copy configuration template:
```bash
cp config_template.yaml config.yaml
```
2. Edit `config.yaml` and fill in your API key:
```yaml
llm:
api_key: "YOUR_DEEPSEEK_API_KEY" # Required
```

## Usage
### Quick Start
```python
from extraction import MetaExtractionPipeline

Initialize pipeline
pipeline = MetaExtractionPipeline("config.yaml")

Run full extraction
results = pipeline.run_full_pipeline(
input_dir="./input_papers",
output_dir="./output"
)

print(f"Extracted {results['final_rows']} data points from {results['studies']} papers")
```

### Stage-by-Stage Execution
```python
### Stage 0: Metadata extraction
overview, anchors = pipeline.stage0_metadata(["paper1.md", "paper2.md"])

### Stage 1: Data extraction
df_stage1 = pipeline.stage1_extraction(anchors)

### Stage 1.5: Cleaning and deduplication
df_cleaned = pipeline.stage1_5_cleaning(df_stage1)

### Stage 2: Validation and gap filling
df_final = pipeline.stage2_validation(df_cleaned)
```

## Output Files

- `stage0_overview.csv`: Study metadata (authors, pig breeds, growth stage, etc.)
- `stage0_anchors.csv`: Located data anchors (tables and text blocks)
- `stage1_outcomes.csv`: Raw extracted data points
- `stage1.5_cleaned.csv`: Cleaned and deduplicated data
- `stage2_final.csv`: **Final validated output**
- `stage2_coverage_report.csv`: Extraction coverage by study

## Pipeline Stages

### Stage 0: Metadata Extraction
- Extracts study-level information (country, authors, pig breeds, etc.)
- Identifies outcome metrics (ADG, ADFI, FCR, G/F)
- Extracts document-wide sample size rules

### Stage 1: Data Extraction
- **Table-first strategy**: Exhaustive extraction from Markdown/HTML tables
- **Text supplement**: Keyword-based paragraph extraction
- **Smart sample size inference**: Automatic pen/pig distinction

### Stage 2: Enhanced Cleaning
- Multi-level priority deduplication:
  1. Table + pen (non-computed) - Highest reliability
  2. Computed values (pen or pig)
  3. Text + pen (non-computed)
  4. Others
- Sample size consistency enforcement (Csample = Tsample)
- Pen/pig conflict detection

### Stage 3: Validation & Gap Filling
- Rule-based validation (zero-cost fast checks)
- Targeted LLM gap filling for high-risk rows only
- Final pen/pig range verification

## Key Algorithms
### Smart n_unit Inference
```python
def infer_n_unit_smart(dwide: Dict, study: str) -> str:
"""
Inference priority:
1. Numeric range rules (rep=3-20, ppr=4-30)
2. Keyword matching (pen/replicate vs pig/animal)
3. LLM extraction (low priority)
4. Default: 'pen'
"""
```

### Enhanced Sample Size Backfilling
```python
def try_fill_n_from_docwide_enhanced(row: Dict, study: str, dwide: Dict) -> Dict:
"""
5-level priority:
1. Existing table values (no override)
2. Exact group name matching
3. Synonym matching
4. Global rules
5. Computed values (validated)
"""
```

## Configuration Parameters

### Concurrency Control
- `max_workers_files`: File-level parallelism (default: 16)
- `max_workers_calls`: LLM call parallelism (default: 16)
- `llm_rate_cap_per_min`: Rate limit (default: 60 calls/min)

### Extraction Parameters
- `max_anchors_per_doc`: Max data anchors per document (default: 40)
- `pad_around_anchor`: Context window padding (default: 1200 chars)
- `docwide_char_limit`: Document-wide context limit (default: 96000 chars)

## Logging

Detailed logs are saved in the `logs/` directory:

- `stage_calls_*.csv`: Per-call LLM diagnostics
- `usage_*.csv`: API usage summary
- `stage2_validation_*.csv`: Validation results
- `stage1.5_quality_*.csv`: Cleaning quality metrics
- `docwide_extraction_*.csv`: Document-wide extraction results



