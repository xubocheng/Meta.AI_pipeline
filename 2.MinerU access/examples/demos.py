"""
Demo script showing how to use the extraction pipeline.
"""

from extraction import MetaExtractionPipeline

# Method 1: Full pipeline
print("="*80)
print("Method 1: Run full pipeline")
print("="*80)

pipeline = MetaExtractionPipeline("config.yaml")
results = pipeline.run_full_pipeline()

print(f"\n✓ Extracted {results['final_rows']} rows from {results['studies']} studies")

# Method 2: Stage-by-stage
print("\n" + "="*80)
print("Method 2: Run stage by stage")
print("="*80)

pipeline2 = MetaExtractionPipeline("config.yaml")

# Stage 0
df_overview, df_anchors = pipeline2.stage0_metadata()
print(f"Stage 0: {len(df_overview)} studies, {len(df_anchors)} anchors")

# Stage 1
df_stage1 = pipeline2.stage1_extraction(df_anchors)
print(f"Stage 1: {len(df_stage1)} rows extracted")

# Stage 1.5
df_cleaned = pipeline2.stage1_5_cleaning(df_stage1)
print(f"Stage 1.5: {len(df_cleaned)} rows after cleaning")

# Stage 2
df_final = pipeline2.stage2_validation(df_cleaned)
print(f"Stage 2: {len(df_final)} final rows")

# Access data
print("\n" + "="*80)
print("Accessing extracted data")
print("="*80)

# Show sample outcomes
print("\nSample outcomes (first 5 rows):")
print(df_final[["Study", "Outcome_key", "Treatment", "Cmean", "Tmean", "Csample"]].head())

# Statistics by outcome
print("\nOutcomes distribution:")
print(df_final["Outcome_key"].value_counts())

# Studies with most data points
print("\nTop 5 studies by data points:")
study_counts = df_final["Study"].value_counts().head(5)
for study, count in study_counts.items():
    print(f"  {study}: {count} rows")

# Sample size completeness
sample_complete = (
    df_final["Csample"].astype(str).str.strip().astype(bool) & 
    df_final["Tsample"].astype(str).str.strip().astype(bool)
).sum()
print(f"\nSample size completeness: {sample_complete}/{len(df_final)} ({sample_complete/len(df_final)*100:.1f}%)")

# SD completeness
sd_complete = (
    df_final["Csd"].astype(str).str.strip().astype(bool) & 
    df_final["Tsd"].astype(str).str.strip().astype(bool)
).sum()
print(f"SD completeness: {sd_complete}/{len(df_final)} ({sd_complete/len(df_final)*100:.1f}%)")