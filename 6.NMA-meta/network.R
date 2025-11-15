suppressPackageStartupMessages({
  library(tidyverse)
  library(metafor)  # 随机效应合并
  library(ggraph)
  library(igraph)
  library(yaml)
  library(scales)
  # library(ggrepel) # 可选：用于 repel 文字
})

`%||%` <- function(x, y) if (is.null(x)) y else x

# ---------- 读取配置 ----------
read_config <- function(path) {
  if (!file.exists(path)) stop("未找到配置文件：", path)
  cfg <- yaml::read_yaml(path)
  
  cfg$effect$measure <- match.arg((cfg$effect$measure %||% "SMD"), c("SMD","WMD"))
  cfg$effect$model$method <- (cfg$effect$model$method %||% "REML")
  cfg$effect$digits <- cfg$effect$digits %||% 2
  
  cfg$samples$per_study      <- cfg$samples$per_study      %||% "treatments_plus_control"
  cfg$samples$pair_aggregate <- cfg$samples$pair_aggregate %||% "sum"
  
  cfg$thresholds$min_studies_per_treatment      <- cfg$thresholds$min_studies_per_treatment      %||% 1
  cfg$thresholds$min_total_sample_per_treatment <- cfg$thresholds$min_total_sample_per_treatment %||% 0
  
  cfg$plot$layout <- cfg$plot$layout %||% "fr"
  cfg$plot$node_fill <- cfg$plot$node_fill %||% "#C7E2F3"
  cfg$plot$node_border <- cfg$plot$node_border %||% "white"
  cfg$plot$node_alpha <- cfg$plot$node_alpha %||% 1
  cfg$plot$edge_color <- cfg$plot$edge_color %||% "grey25"
  cfg$plot$edge_alpha <- cfg$plot$edge_alpha %||% 0.9
  cfg$plot$node_size_range <- cfg$plot$node_size_range %||% c(18,55)
  cfg$plot$edge_width_range<- cfg$plot$edge_width_range %||% c(0.6,3.5)
  cfg$plot$show_effect_labels <- cfg$plot$show_effect_labels %||% TRUE
  cfg$plot$label_size_effect  <- cfg$plot$label_size_effect  %||% 4.0
  cfg$plot$node_label_size    <- cfg$plot$node_label_size    %||% 5.0
  cfg$plot$node_label_fontface<- cfg$plot$node_label_fontface%||% "bold"
  cfg$plot$title <- cfg$plot$title %||% "Network Meta-Analysis"
  cfg$plot$subtitle <- cfg$plot$subtitle %||% ""
  cfg$plot$caption <- cfg$plot$caption %||% ""
  cfg$plot$seed <- cfg$plot$seed %||% 42
  cfg$plot$width <- cfg$plot$width %||% 10
  cfg$plot$height <- cfg$plot$height %||% 10
  cfg$plot$dpi <- cfg$plot$dpi %||% 300
  
  cfg$plot$legend_text_size  <- cfg$plot$legend_text_size  %||% 12
  cfg$plot$legend_title_size <- cfg$plot$legend_title_size %||% 13
  cfg$plot$legend_key_height <- cfg$plot$legend_key_height %||% 0.8
  cfg$plot$legend_key_width  <- cfg$plot$legend_key_width  %||% 1.2
  
  cfg$output$edge_table_csv <- cfg$output$edge_table_csv %||% "pairwise_summary.csv"
  cfg$output$plot_file <- cfg$output$plot_file %||% "network_plot.png"
  cfg$output$pdf_file  <- cfg$output$pdf_file  %||% NULL
  
  cfg$input$control_regex <- cfg$input$control_regex %||% "^(A|control|ctrl|placebo)$"
  
  cfg
}

stop_if_missing_cols <- function(df, cols) {
  miss <- setdiff(cols, names(df))
  if (length(miss) > 0) stop("CSV 缺少如下列：", paste(miss, collapse = ", "))
}

# ---------- 计算单条研究的效应量与方差（metafor::escalc） ----------
compute_es <- function(df, measure){
  measure2 <- if (measure == "SMD") "SMD" else "MD"
  # m1i/sd1i/n1i = Treatment, m2i/sd2i/n2i = Control
  metafor::escalc(measure = measure2,
                  m1i = Tmean, sd1i = Tsd, n1i = Tsample,
                  m2i = Cmean, sd2i = Csd, n2i = Csample,
                  data = df, vtype = "UB") %>%
    as_tibble()
}

# ---------- 对每个添加剂做随机效应合并（失败时回退 FE） ----------
safe_rma <- function(yi, vi, method){
  k <- sum(is.finite(yi) & is.finite(vi))
  if (k == 0) return(list(mu = NA_real_, se = NA_real_, tau2 = NA_real_, k=0))
  try_fit <- try(metafor::rma.uni(yi = yi, vi = vi, method = method), silent = TRUE)
  if (inherits(try_fit, "try-error") || is.null(try_fit$b)) {
    try_fit <- metafor::rma.uni(yi = yi, vi = vi, method = "FE")
  }
  list(mu = as.numeric(try_fit$b[1]),
       se = try_fit$se,
       tau2 = try_fit$tau2 %||% NA_real_,
       k = k)
}

# —— 生成布局表：返回包含节点坐标的 layout_tbl —— #
choose_layout_tbl <- function(g, cfg){
  lay <- tolower(cfg$plot$layout %||% "fr")
  if (lay == "circle") {
    ggraph::create_layout(g, layout = "linear", circular = TRUE)
  } else {
    ggraph::create_layout(g, layout = lay)
  }
}

# —— 计算边的中点与法向偏移，仅用于“效应量”标签 —— #
build_edge_label_positions <- function(g, layout_tbl, cfg, nudge = 0.04){
  # 1) 节点坐标
  node_pos <- layout_tbl %>% as_tibble() %>% dplyr::select(name, x, y)
  
  # 2) 边 + 坐标
  e <- igraph::as_data_frame(g, what = "edges") %>%
    dplyr::left_join(node_pos, by = c("from" = "name")) %>% dplyr::rename(x1 = x, y1 = y) %>%
    dplyr::left_join(node_pos, by = c("to"   = "name")) %>% dplyr::rename(x2 = x, y2 = y)
  
  # 3) 中点 + 单位法向（只保留上方一条标签）
  e %>%
    dplyr::mutate(
      mx = (x1 + x2) / 2, my = (y1 + y2) / 2,
      dx = x2 - x1, dy = y2 - y1,
      len = sqrt(dx*dx + dy*dy) + 1e-9,
      nx = -dy/len, ny = dx/len,
      off = nudge
    ) %>%
    dplyr::transmute(
      x = mx + nx*off,
      y = my + ny*off,
      label = sprintf("%.*f", cfg$effect$digits, abs(effect_value))
    )
}

# ---------- 主流程 ----------
args <- commandArgs(trailingOnly = TRUE)
cfg <- read_config(if (length(args) >= 1) args[1] else "netmeta.yaml")

# 读取
dat_raw <- readr::read_csv(cfg$input$csv_path, show_col_types = FALSE)

# 必要列
required_cols <- c("Study","class","Outcome","new_timepoint",
                   "Csample","Cmean","Csd","Tsample","Tmean","Tsd")
stop_if_missing_cols(dat_raw, required_cols)

# 过滤
dat <- dat_raw
if (!is.null(cfg$input$filters$Outcome) && length(cfg$input$filters$Outcome) > 0) {
  dat <- dat %>% filter(.data$Outcome %in% cfg$input$filters$Outcome)
}
if (!is.null(cfg$input$filters$new_timepoint) && length(cfg$input$filters$new_timepoint) > 0) {
  dat <- dat %>% filter(.data$new_timepoint %in% cfg$input$filters$new_timepoint)
}

# 剔除对照（不作为节点）
control_rx <- cfg$input$control_regex
dat <- dat %>%
  filter(!is.na(class)) %>%
  mutate(class_lc = tolower(trimws(class))) %>%
  filter(!str_detect(class_lc, regex(control_rx, ignore_case = TRUE))) %>%
  select(-class_lc)

if (nrow(dat) == 0) stop("过滤后没有可用的处理组数据。")

# 计算每条研究的效应量与方差
es <- compute_es(dat, measure = cfg$effect$measure)

# 每条研究的样本量（用于后续统计）
es <- es %>% mutate(study_sample = Tsample + Csample)

# 每个添加剂内做随机效应合并（一次拟合，避免重复）
meta_by_class <- es %>%
  group_by(class) %>%
  summarise(
    {
      fit <- safe_rma(yi, vi, method = cfg$effect$model$method)
      tibble(mu = fit$mu, se = fit$se, tau2 = fit$tau2)
    },
    k = n_distinct(Study),
    total_sample = sum(study_sample, na.rm = TRUE),
    .groups = "drop"
  )

# 按阈值筛选参与网络的添加剂
meta_by_class <- meta_by_class %>%
  filter(k >= cfg$thresholds$min_studies_per_treatment,
         total_sample >= cfg$thresholds$min_total_sample_per_treatment)

if (nrow(meta_by_class) < 2) {
  stop("满足阈值的添加剂数 < 2，无法构建网络。请降低 thresholds 或放宽过滤条件。")
}

# 两两配对：效应差 = μ_i − μ_j；SE_diff ≈ sqrt(se_i^2 + se_j^2)
pairs <- t(combn(meta_by_class$class, 2))
pair_df <- tibble(Treatment1 = pairs[,1], Treatment2 = pairs[,2]) %>%
  left_join(meta_by_class, by = c("Treatment1"="class")) %>%
  rename(mu1 = mu, se1 = se, k1 = k, sample1 = total_sample) %>%
  left_join(meta_by_class, by = c("Treatment2"="class")) %>%
  rename(mu2 = mu, se2 = se, k2 = k, sample2 = total_sample) %>%
  mutate(
    SMD_different = mu1 - mu2,
    SE_diff = sqrt(se1^2 + se2^2),
    CI_low  = SMD_different - 1.96*SE_diff,
    CI_high = SMD_different + 1.96*SE_diff,
    sample_n = case_when(
      cfg$samples$pair_aggregate == "sum"  ~ sample1 + sample2,
      cfg$samples$pair_aggregate == "min"  ~ pmin(sample1, sample2),
      cfg$samples$pair_aggregate == "mean" ~ (sample1 + sample2)/2,
      TRUE ~ sample1 + sample2
    ),
    study_n  = case_when(
      cfg$samples$pair_aggregate == "sum"  ~ k1 + k2,
      cfg$samples$pair_aggregate == "min"  ~ pmin(k1, k2),
      cfg$samples$pair_aggregate == "mean" ~ (k1 + k2)/2,
      TRUE ~ k1 + k2
    )
  ) %>%
  select(Treatment1, Treatment2, SMD_different, sample_n, study_n, CI_low, CI_high)

# 导出两两差值表
readr::write_csv(pair_df, cfg$output$edge_table_csv)

# —— 准备节点与边供作图 —— #
node_df <- meta_by_class %>%
  transmute(name = class,
            study_count = k,
            total_sample = total_sample)

edge_df <- pair_df %>%
  transmute(
    class1 = pmin(Treatment1, Treatment2),
    class2 = pmax(Treatment1, Treatment2),
    effect_value = SMD_different,
    total_sample = sample_n,
    n_studies = study_n,
    effect_label = sprintf("%s−%s: %.*f", class1, class2, cfg$effect$digits, abs(effect_value))
  )

# —— 构图 —— #
g <- igraph::graph_from_data_frame(
  d = edge_df %>% select(from = class1, to = class2,
                         effect_value, total_sample, effect_label, n_studies),
  vertices = node_df,
  directed = FALSE
)

# —— 使用同一布局生成坐标，并计算“效应量”标签位置 —— #
set.seed(cfg$plot$seed)
layout_tbl <- choose_layout_tbl(g, cfg)
lab_pos <- build_edge_label_positions(
  g, layout_tbl, cfg,
  nudge = (cfg$plot$label_nudge %||% 0.04)  # 若 YAML 未配置则用 0.04
)

# —— 绘图（仅叠加上方效应量标签） —— #
p <- ggraph::ggraph(layout_tbl) +
  # 边：粗细=样本量
  ggraph::geom_edge_link(ggplot2::aes(width = total_sample),
                         lineend = "round",
                         colour = cfg$plot$edge_color,
                         alpha  = cfg$plot$edge_alpha,
                         show.legend = TRUE) +
  ggraph::scale_edge_width(range = cfg$plot$edge_width_range, name = "Smaple k") +
  
  # 节点：浅蓝圆形
  ggraph::geom_node_point(ggplot2::aes(size = study_count),
                          shape = 21,
                          fill  = cfg$plot$node_fill,
                          colour = cfg$plot$node_border,
                          stroke = 1,
                          alpha  = cfg$plot$node_alpha,
                          show.legend = TRUE) +
  ggplot2::scale_size(range = cfg$plot$node_size_range, name = "Study N") +
  
  # 只显示上方“效应量”标签
  { if (isTRUE(cfg$plot$show_effect_labels))
    ggplot2::geom_text(data = lab_pos,
                       ggplot2::aes(x = x, y = y, label = label),
                       size = cfg$plot$label_size_effect,
                       colour = cfg$plot$edge_color)
    else NULL } +
  
  # 节点文字
  ggraph::geom_node_text(ggplot2::aes(label = name),
                         size = cfg$plot$node_label_size,
                         fontface = cfg$plot$node_label_fontface,
                         repel = TRUE) +
  
  ggplot2::labs(title = cfg$plot$title,
                subtitle = cfg$plot$subtitle,
                caption  = cfg$plot$caption) +
  ggplot2::theme_void(base_size = 12) +
  ggplot2::theme(
    legend.position = "right",
    # ↓ 图例文字大小（来自你的 YAML 配置）
    legend.text  = ggplot2::element_text(size = cfg$plot$legend_text_size),
    legend.title = ggplot2::element_text(size = cfg$plot$legend_title_size),
    legend.key.height = grid::unit(cfg$plot$legend_key_height, "lines"),
    legend.key.width  = grid::unit(cfg$plot$legend_key_width,  "lines"),
    # ↓ 其余标题样式
    plot.title    = ggplot2::element_text(face = "bold", hjust = 0.5),
    plot.subtitle = ggplot2::element_text(hjust = 0.5)
  )



# —— 保存：位图版本（由扩展名决定设备，例如 .png/.jpg/.svg） —— #
ggplot2::ggsave(
  filename = cfg$output$plot_file,
  plot = p,
  width = cfg$plot$width,
  height = cfg$plot$height,
  dpi = cfg$plot$dpi,
  units = "in"
)

# —— 保存：PDF 版本（矢量，适合打印/期刊） —— #
pdf_path <- cfg$output$pdf_file %||% sub("(?i)\\.[a-z0-9]+$", ".pdf", cfg$output$plot_file, perl = TRUE)
ggplot2::ggsave(
  filename = pdf_path,
  plot = p,
  width = cfg$plot$width,
  height = cfg$plot$height,
  units = "in"   # PDF 不需要 dpi
)

message("✅ 已导出两两差值表：", cfg$output$edge_table_csv)
message("✅ 网络图已保存（位图）：", cfg$output$plot_file)
message("✅ 网络图已保存（PDF）：", pdf_path)
