"""
Test script to verify pipeline functionality.
"""

import os
import tempfile
import shutil
from extraction import MetaExtractionPipeline

def create_test_file(path: str, content: str):
    """Create a test markdown file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def test_basic_pipeline():
    """Test basic pipeline with sample data."""
    print("="*80)
    print("Testing Basic Pipeline")
    print("="*80)
    
    # Create temporary directories
    temp_dir = tempfile.mkdtemp()
    input_dir = os.path.join(temp_dir, "input")
    output_dir = os.path.join(temp_dir, "output")
    
    try:
        # Create sample markdown file
        sample_content = """
# Growth Performance of Pigs Fed with Additive

## Materials and Methods

A total of 48 crossbred pigs (initial BW 25.3 kg) were used in this 28-d study.
Pigs were randomly assigned to 2 treatments with 6 replicates per treatment 
and 4 pigs per replicate (pen).

## Results

| Item | Control | Treatment | SEM | P-value |
|------|---------|-----------|-----|---------|
| ADG, g/d | 450.2 | 478.5 | 15.3 | 0.04 |
| ADFI, g/d | 1250.3 | 1280.7 | 35.2 | 0.35 |
| FCR | 2.78 | 2.68 | 0.12 | 0.32 |

ADG = average daily gain; ADFI = average daily feed intake; FCR = feed conversion ratio.
n = 6 pens per treatment, 4 pigs per pen.
"""
        
        test_file = os.path.join(input_dir, "test_study.md")
        create_test_file(test_file, sample_content)
        
        print(f"\nCreated test file: {test_file}")
        print(f"Input directory: {input_dir}")
        print(f"Output directory: {output_dir}")
        
        # Create minimal config
        config_content = f"""
llm:
  api_key: "YOUR_API_KEY_HERE"
  base_url: "https://api.deepseek.com"
  api_path: "/v1/chat/completions"
  model: "deepseek-chat"
  temperature: 0.0
  timeout_s: 150
  max_tokens: 3000

paths:
  input_dir: "{input_dir}"
  output_dir: "{output_dir}"
  logs_dir: "{os.path.join(output_dir, 'logs')}"

concurrency:
  max_workers_files: 2
  max_workers_calls: 2
  llm_rate_cap_per_min: 10

extraction:
  max_anchors_per_doc: 20
  pad_around_anchor: 800
  chars_limit_metadata: 20000
  chars_limit_anchor: 10000
  chars_limit_stage2: 15000
  docwide_char_limit: 80000
  docwide_eager_apply_samples: true
  enable_overlap_suppression: true
  overlap_threshold: 0.6

outcomes:
  canonical_set: ["ADG", "ADFI", "FCR", "G/F"]
  synonyms:
    ADG: ["adg", "average daily gain"]
    ADFI: ["adfi", "average daily feed intake"]
    FCR: ["fcr", "feed conversion ratio"]
    G/F: ["g/f", "gain/feed"]
  default_keywords: ["adg", "adfi", "fcr", "table", "results"]

validation:
  max_fillmissing_rows: 50
  enable_rule_validation: true
  enable_llm_validation: true

output:
  encoding: "utf-8-sig"
  write_timestamped_copies: true
  overview_cols:
    - "Study"
    - "Country"
    - "Authors"
    - "pig_breeds"
    - "n_unit_preferred"
    - "Outcomes_list_extract"
    - "doc_hash"

logging:
  level: "INFO"
  format: "%(asctime)s [%(levelname)s] %(message)s"
  console_output: true
  file_output: true
"""
        
        config_path = os.path.join(temp_dir, "test_config.yaml")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(config_content)
        
        print(f"\nCreated config: {config_path}")
        
        # Initialize pipeline
        print("\n" + "-"*80)
        print("Initializing pipeline...")
        pipeline = MetaExtractionPipeline(config_path)
        
        # Note: This will fail without valid API key
        print("\nNote: Pipeline requires valid DeepSeek API key to run")
        print("Update config.yaml with your API key to test full functionality")
        
        print("\n✓ Test setup complete")
        print(f"\nTo run manually:")
        print(f"1. Edit {config_path} and add your API key")
        print(f"2. Run: python -c \"from extraction import MetaExtractionPipeline; ")
        print(f"   p = MetaExtractionPipeline('{config_path}'); p.run_full_pipeline()\"")
        
    finally:
        # Cleanup
        print(f"\nCleaning up temporary files: {temp_dir}")
        shutil.rmtree(temp_dir)
    
    print("\n✓ Test completed successfully")

if __name__ == "__main__":
    test_basic_pipeline()