### The following only presents a portion of the prompts and R code for the LLM-Powered Pipeline for Comprehensive Meta-Analysis in Animal Nutrition.

---

# LLM-Powered Pipeline for Comprehensive Meta-Analysis in Animal Nutrition

This repository collects prompt templates and companion scripts used in an LLM-powered workflow for screening, extracting and analyzing animal nutrition studies (e.g., growth performance outcomes in pigs).

Only a subset of prompts and implementation details is included here for illustration. No raw or individual-level data are shared.

---

## Repository structure (by folder order 1 → 6)

### 1. `1.screen strategy/`

**Purpose:** LLM-based abstract screening, summarization and relevance assessment.

- **`search_strategy.py`**
    
    - Defines prompt templates and helper functions to:
        
        - Decide whether a given abstract needs a **full** vs **concise** summary.
            
        - Generate:
            
            - A two-sentence, high-level technical summary of the key findings.
                
            - A shorter one-sentence/concise summary.
                
        - Judge whether a study is **relevant** to a given research topic/direction and extract key linking keywords.
            
    - The script assumes you provide your own LLM backend via a function like `chat_completion(prompt: str) -> response`, which is **not** included here (you can hook it to OpenAI, Azure, or a local model).
        

> This folder focuses on prompts and control logic for title/abstract level screening in an LLM-assisted pipeline.

---

### 2. `2.MinerU access/`

**Purpose:** Access full-text PDFs via the MinerU document parsing pipeline.

- **`demo.py`**
    
    - Adapted example script (from the MinerU project) showing how to:
        
        - Prepare the MinerU environment.
            
        - Call different backends (e.g. `pipeline`, `vlm`) to parse PDFs.
            
        - Batch-parse PDF files into:
            
            - Markdown
                
            - Intermediate JSON
                
            - Model outputs
                
            - Optional visualization files with layout/table/element bounding boxes.
                
    - Demonstrates configuration of:
        
        - Language options
            
        - Parsing method
            
        - Formula/table extraction switches
            
        - Output directory and export options
            

> Use this as a reference for integrating MinerU into an LLM-centered pipeline that turns PDFs into structured text for downstream extraction.  
> Please follow the official MinerU documentation for installation and licensing.

---

### 3. `3.forest figure/`

**Purpose:** Config-driven R workflow for generating customized forest plots for meta-analysis.

- **`forest_figure.R`**
    
    - R script that:
        
        - Reads a YAML configuration file.
            
        - Loads study-level aggregated data (e.g., means, SDs, sample sizes).
            
        - Fits random-effects meta-analyses (e.g., via `meta`, `metafor`) for multiple outcomes.
            
        - Produces publication-ready forest plots with flexible layout and styling.
            
- **`forest figure.yaml`**
    
    - Configuration template specifying:
        
        - **Input/output**
            
            - Relative paths (e.g. `.`) and CSV filename placeholder.
                
        - **Outcomes**
            
            - Example outcomes: `ADG`, `ADFI`, `G/F` (can be customized).
                
        - **Typography & layout**
            
            - Global base font size, label sizes, title size.
                
            - Left/middle/right panel widths and alignment.
                
        - **Plot styling**
            
            - Forest background color, transparency, and rounded-corner radius.
                
            - Axes visibility and reference line settings.
                
            - Options to align the main title with the classification column.
                
            - PDF export switches.
                

> All paths are relative and filenames are placeholders; you must plug in your own sanitized CSV data locally.

---

### 4. `4.plot figure/`

**Purpose:** Additional R visualizations (scatter / bubble / panelled plots) for growth performance outcomes under different feed additives and timepoints.

Each plotting script follows a similar pattern:

1. Read YAML config.
    
2. Load a CSV file of aggregated meta-analysis results.
    
3. Derive effect sizes (e.g., Hedges’ g) or use pre-computed summary estimates.
    
4. Generate publication-ready figures (PNG/PDF).
    

Folders and files:

- **`plot1.R` + `plot1.yaml`**
    
    - Computes or uses standardized mean differences (e.g., for ADG/ADFI/G/F).
        
    - Produces scatter-style visualizations where:
        
        - Axes are specific outcomes (e.g. ADFI vs ADG).
            
        - Point color encodes additive **class** (e.g., Probiotics, Enzymes, Herbal, etc.).
            
        - Point size or shape can reflect sample size or other grouping variables.
            
    - YAML controls:
        
        - Outcome filtering.
            
        - Color palettes per class.
            
        - Output directory and file names.
            
        - Basic theme (font sizes, legend position, axis labels).
            
- **`plot2.R` + `plot2.yaml`**
    
    - Focuses on **ADG vs ADFI** scatter plots across additive classes and growth stages (e.g., piglet, growing pig, finishing pig).
        
    - YAML defines:
        
        - Which classes and timepoints to keep.
            
        - Mapping from timepoint → point size.
            
        - Detailed theme settings (grid lines, zero lines, padding, axis labels, title/subtitle).
            
- **`plot3.R` + `plot3.yaml`**
    
    - Bubble plot centered on **G/F (gain-to-feed ratio)**.
        
    - YAML includes:
        
        - I/O paths and exported summary CSV.
            
        - Bubble size and color mappings (e.g., by class and stage).
            
        - Device settings (width, height, dpi).
            
        - A coherent theme for legends, axis text, and panel layout.
            

> These scripts are general templates: they do **not** include raw data and do not assume any specific institution or trial identifiers.

---

### 5. `5.netmeta_work/`

**Purpose:** Configuration-driven network meta-analysis (frequentist-style) and network visualization.

- **`netmeta.R`**
    
    - Uses `tidyverse`, `metafor`, `ggraph`, `igraph`, `yaml`, `scales`, etc.
        
    - Main capabilities:
        
        - Read a YAML config (`netmeta.yaml`) and an aggregated CSV dataset.
            
        - Filter studies by:
            
            - Outcome (e.g. Lipid, Protein, etc.).
                
            - Timepoint (`new_timepoint`, e.g. baseline, 12w).
                
        - Identify control arms by a regex (e.g., `^(A|control|ctrl|placebo)$`).
            
        - Construct pairwise comparisons and effect sizes.
            
        - Build a treatment network where:
            
            - Node size/labels reflect total sample size and treatment identity.
                
            - Edge width/opacity reflect the amount of evidence between treatments.
                
        - Perform random-effects meta-analysis and summarize results.
            
        - Export summary tables and network plots.
            
- **`netmeta.yaml`**
    
    - Key sections:
        
        - `input`: CSV path and filters.
            
        - `effect`: choice of effect model (e.g., REML, DL, ML, HS, SJ, FE).
            
        - `thresholds`: minimum number of studies and total sample size per treatment.
            
        - `plot`: layout algorithm (e.g. Fruchterman–Reingold), node/edge styles, label sizes.
            
        - `samples`: how to aggregate sample and study counts at the edge level (e.g., sum / min / mean).
            

> This component is suitable for building treatment networks and performing evidence synthesis across multiple feed additives.

---

### 6. `6.NMA-meta/`

**Purpose:** Network meta-analysis (NMA) with an additional classical R example for Bayesian modeling.

This folder contains:

- **`network.R` / `network.yaml`**
    
    - An alternative entry point that mirrors the configuration-driven network construction and plotting used in `5.netmeta_work/`.
        
    - Uses the same ideas:
        
        - YAML-based configuration.
            
        - Construction of a treatment network.
            
        - Visualization and summary of network structure.
            
- **`*.NMA.R` (example script)**
    
    - A traditional tutorial-style script for Bayesian NMA using:
        
        - `gemtc` (Bayesian NMA framework).
            
        - `rjags` (Gibbs sampler backend).
            
        - Optional helper packages such as `dmetar`, `showtext`.
            
    - Walks through:
        
        - Reading the NMA dataset from CSV.
            
        - Defining treatments and building the `mtc.network`.
            
        - Plotting the evidence network (node sizes by sample size, edges by number of direct comparisons).
            
        - Setting up and running a Bayesian model for NMA.
            

> This folder is primarily pedagogical, showing how classical NMA scripts can be integrated or complemented within an LLM-augmented workflow.

---

## How to use this repository

1. **Hook up your LLM backend (Python)**
    
    - Implement a `chat_completion()` function that sends prompts from `1.screen strategy/search_strategy.py` to your chosen model and returns the text output.
        
    - Apply the screening prompts to titles/abstracts to:
        
        - Generate consistent summaries.
            
        - Filter for relevance before investing effort in full-text extraction.
            
2. **Parse PDFs to structured text (Python + MinerU)**
    
    - Install MinerU and its dependencies.
        
    - Adapt `2.MinerU access/demo.py`:
        
        - Set your own input PDF directory.
            
        - Customize backend, language settings, and export options.
            
    - Use the markdown/JSON outputs as inputs to manual or LLM-based data extraction steps.
        
3. **Run meta-analysis and create figures (R)**
    
    - Prepare **study-level aggregated datasets** (CSV files) with standardized columns (e.g. treatment, control, means, SDs, sample sizes, outcome, timepoint).
        
    - Edit the YAML files in folders `3`–`6` to:
        
        - Point to your CSV file(s).
            
        - Specify which outcomes, timepoints, and classes to include.
            
        - Adjust visual style to fit your target journal or report.
            
    - Run the corresponding `.R` scripts to produce:
        
        - Forest plots for conventional meta-analysis.
            
        - Scatter/bubble plots linking performance outcomes.
            
        - Network diagrams and NMA summaries.
            

---

## Privacy and data protection

- This repository is **code- and config-only**:
    
    - No raw animal-level data.
        
    - No clinical/experimental identifiers, institution names, or personal information.
        
- All file paths and filenames in YAML files are placeholders (e.g. `"."`, `".csv"`).  
    Users should **keep their own data local** and carefully anonymize anything shared publicly.
    
- When adapting the pipeline, ensure compliance with relevant ethical, regulatory, and data-sharing guidelines for animal experiments.
    

---

## Suggested environment

- **Python** ≥ 3.9
    
    - Typical dependencies: `mineru`, `loguru`, and MinerU’s internal requirements.
        
    - Your own LLM client library (e.g. `openai`) if needed.
        
- **R** ≥ 4.2
    
    - Common packages across scripts:
        
        - Data and config: `yaml`, `readr`, `dplyr`, `tidyr`, `stringr`, `rlang`.
            
        - Meta-analysis: `meta`, `metafor`.
            
        - Plotting: `ggplot2`, `cowplot`, `scales`, `ggraph`, `igraph`, `showtext`.
            
        - Bayesian NMA (optional): `gemtc`, `rjags`, `dmetar`.
            

You can copy this README directly into your GitHub repository and then adjust any wording, package lists, or folder names as your project evolves.