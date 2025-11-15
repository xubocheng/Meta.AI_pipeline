# meta_forest_from_yaml.R
# ------------------------------------------------------------
# 新增特性：
# - 标题与左侧 class 列对齐；主标题可用模板自定义（{OUTCOME}/{METHOD_TAU}）
# - 左/中/右面板列名从 YAML 自定义
# - “列名-内容”分隔横线：支持全局统一一条线（无断点，可调位置/粗细/颜色）
# - 森林图：圆角背景（颜色/透明度/圆角 YAML 控制），显示 x/y 轴
# - 背景仅填充坐标轴内第一/第二象限（不盖表头/外部）
# - 左/右面板列宽/对齐、三面板宽度 YAML 控制
# ------------------------------------------------------------

suppressPackageStartupMessages({
  req_pkgs <- c(
    "yaml", "readr", "dplyr", "tidyr", "stringr",
    "meta", "metafor", "ggplot2", "cowplot", "rlang"
  )
  to_install <- req_pkgs[!req_pkgs %in% rownames(installed.packages())]
  if (length(to_install) > 0) install.packages(to_install, quiet = TRUE)
  lapply(req_pkgs, require, character.only = TRUE)
})

`%||%` <- function(x, y) if (is.null(x)) y else x
ensure_dir <- function(path) { if (!dir.exists(path)) dir.create(path, recursive = TRUE) }
num_or_na  <- function(x) suppressWarnings(as.numeric(x))

safe_metacont <- function(df, sm = c("MD", "SMD"), method_tau = "REML", sd_floor = 1e-8) {
  sm <- match.arg(sm)
  
  # 清洗 + 下限保护，避免 0/极小方差引发数值不稳
  df <- df |>
    dplyr::mutate(
      Csample = num_or_na(Csample), Tsample = num_or_na(Tsample),
      Cmean   = num_or_na(Cmean),   Tmean   = num_or_na(Tmean),
      Csd     = pmax(num_or_na(Csd), sd_floor),
      Tsd     = pmax(num_or_na(Tsd), sd_floor)
    ) |>
    dplyr::filter(
      !is.na(Csample) & !is.na(Tsample) & Csample > 0 & Tsample > 0,
      !is.na(Cmean)   & !is.na(Tmean),
      !is.na(Csd)     & !is.na(Tsd)
    )
  
  if (nrow(df) < 2) return(NULL)
  
  fit_once <- function(mtau, hk = TRUE) {
    suppressWarnings(try(
      meta::metacont(
        n.e = Tsample, mean.e = Tmean, sd.e = Tsd,
        n.c = Csample, mean.c = Cmean, sd.c = Csd,
        data = df, studlab = Study,
        sm = sm, random = TRUE, fixed = FALSE,
        method.tau = mtau, hakn = hk, method.smd = "Hedges"
      ),
      silent = TRUE
    ))
  }
  
  # 依次尝试多种 τ² 估计器；必要时关闭 HK
  cand_methods <- unique(c(method_tau, "DL", "SJ", "HE", "PM", "ML"))
  fit <- NULL
  for (mt in cand_methods) {
    fit <- fit_once(mt, hk = TRUE)
    if (!inherits(fit, "try-error")) break
    fit <- fit_once(mt, hk = FALSE)
    if (!inherits(fit, "try-error")) break
  }
  
  # 全部随机效应失败 → 固定效应兜底
  if (inherits(fit, "try-error") || is.null(fit)) {
    fit <- suppressWarnings(try(
      meta::metacont(
        n.e = Tsample, mean.e = Tmean, sd.e = Tsd,
        n.c = Csample, mean.c = Cmean, sd.c = Csd,
        data = df, studlab = Study,
        sm = sm, random = FALSE, fixed = TRUE,
        method.smd = "Hedges"
      ),
      silent = TRUE
    ))
    if (inherits(fit, "try-error") || is.null(fit)) return(NULL)
  }
  
  list(
    k     = fit$k,
    TE    = as.numeric(if (!is.null(fit$TE.random)) fit$TE.random else fit$TE.fixed),
    lower = as.numeric(if (!is.null(fit$lower.random)) fit$lower.random else fit$lower.fixed),
    upper = as.numeric(if (!is.null(fit$upper.random)) fit$upper.random else fit$upper.fixed),
    seTE  = as.numeric(if (!is.null(fit$seTE.random)) fit$seTE.random else fit$seTE.fixed),
    pval  = as.numeric(if (!is.null(fit$pval.random)) fit$pval.random else fit$pval.fixed),
    I2    = as.numeric(if (!is.null(fit$I2)) fit$I2 else NA_real_),
    tau2  = as.numeric(if (!is.null(fit$tau2)) fit$tau2 else NA_real_),
    obj   = fit
  )
}

format_ci <- function(est, lo, hi, digits = 2) {
  paste0(sprintf(paste0("%0.", digits, "f"), est),
         " [", sprintf(paste0("%0.", digits, "f"), lo), ", ",
         sprintf(paste0("%0.", digits, "f"), hi), "]")
}
sanitize_filename <- function(x) gsub("[/\\\\:*?\"<>|]", "-", x)
fmt_p <- function(p) {
  if (length(p) == 0) return(character(0))
  p_num <- suppressWarnings(as.numeric(p))
  res <- rep(NA_character_, length(p_num)); res[is.na(p_num)] <- "NA"
  idx <- !is.na(p_num)
  res[idx & p_num < 0.001] <- "<0.001"
  res[idx & p_num >= 0.001] <- sprintf("%.3f", p_num[idx & p_num >= 0.001])
  res
}

# 统一 I² 到百分数（0–100）。若原值是比例(<=1)则 ×100，若已是百分数(>1)则保持。
i2_to_pct <- function(x) {
  out <- suppressWarnings(as.numeric(x))
  ifelse(is.na(out), NA_real_, ifelse(out <= 1, out * 100, out))
}

safe_egger_p <- function(meta_obj) {
  if (is.null(meta_obj)) return(NA_real_)
  if (is.null(meta_obj$k) || meta_obj$k < 3) return(NA_real_)
  out <- try(meta::metabias(meta_obj, method.bias = "linreg"), silent = TRUE)
  if (inherits(out, "try-error")) return(NA_real_)
  p <- out$p.value; if (is.null(p)) p <- out$pval; if (is.null(p)) p <- out$p
  suppressWarnings(as.numeric(p))
}

# -------- 列宽/对齐辅助函数 --------
compute_col_pos <- function(widths, just, pad = 0.02) {
  widths <- as.numeric(widths); n <- length(widths)
  if (length(just) != n) just <- rep(0, n)
  x_min <- c(0, cumsum(widths)[-n]); x_max <- cumsum(widths); x <- numeric(n)
  for (i in seq_len(n)) {
    if (just[i] <= 0) x[i] <- x_min[i] + pad
    else if (just[i] >= 1) x[i] <- x_max[i] - pad
    else x[i] <- (x_min[i] + x_max[i]) / 2
  }
  list(x = x, x_min = x_min, x_max = x_max, xlim = c(0, sum(widths)))
}

# 让向量长度符合期望，不足就补默认值，超出就裁切
normalize_vec_len <- function(x, n, fill = NULL) {
  x <- as.numeric(x)
  if (length(x) >= n) return(x[1:n])
  if (is.null(fill)) fill <- rep(tail(x, 1), n - length(x))
  c(x, fill)[1:n]
}

# -------- 首选顺序工具：把用户给的顺序放前面，剩余的自动补在后面 --------
make_preferred_levels <- function(existing, preferred = NULL) {
  ux <- unique(as.character(existing))
  if (is.null(preferred) || length(preferred) == 0) return(ux)
  c(as.character(preferred), setdiff(ux, as.character(preferred)))
}

# ---------- 主函数 ----------
run_meta_from_yaml <- function(yaml_path = "config.yaml") {
  stopifnot(file.exists(yaml_path))
  cfg <- yaml::read_yaml(yaml_path)
  
  in_path <- cfg$input$path %||% "."
  in_file <- cfg$input$file %||% stop("config.yaml 缺少 input$file")
  out_dir <- cfg$output_dir %||% "meta_output"
  method_tau <- cfg$random_method %||% "REML"
  outcomes_keep <- cfg$outcomes %||% c("ADG", "ADFI", "G/F")
  
  fig_w   <- cfg$figure$width %||% 12
  fig_h   <- cfg$figure$height %||% 9
  fig_dpi <- cfg$figure$dpi %||% 300
  fig_base <- cfg$figure$base_size %||% 16
  lab_sz   <- cfg$figure$label_size %||% 5
  title_sz <- cfg$figure$title_size %||% 28
  
  pt_rgb <- cfg$figure$point_rgb %||% c(0, 0, 0)
  if (length(pt_rgb) != 3) pt_rgb <- c(0, 0, 0)
  point_color <- do.call(rgb, as.list(c(pt_rgb, maxColorValue = 255)))
  point_size  <- cfg$figure$point_size %||% 3
  
  err_lwd   <- cfg$figure$err_linewidth   %||% 1.1
  vline_lwd <- cfg$figure$vline_linewidth %||% 0.8
  
  # ------- 布局/文本参数（YAML 控制） -------
  panel_widths <- cfg$figure$panel_widths %||% c(1.6, 1.0, 1.6)
  
  left_cols_width <- cfg$figure$left_cols_width %||% c(0.70, 0.35, 1.05)
  left_cols_just  <- cfg$figure$left_cols_just  %||% c(0, 0.5, 0)
  right_cols_width <- cfg$figure$right_cols_width %||% c(1.05, 0.45, 0.55)
  right_cols_just  <- cfg$figure$right_cols_just  %||% c(0, 0.5, 0.5)
  col_padding <- cfg$figure$col_padding %||% 0.02
  
  # 列名（可自定义）
  left_headers  <- cfg$figure$left_headers  %||% c("class", "k", "WMD (95% CI)")
  right_headers <- cfg$figure$right_headers %||% c("SMD (95% CI)", "I\u00B2", "P(Egger)")
  
  # 主标题模板（支持 {OUTCOME} / {METHOD_TAU}）
  title_template <- cfg$figure$title_template %||%
    "Meta-analysis ({OUTCOME}) \u2014 Random-effects {METHOD_TAU} (Knapp-Hartung)"
  
  # 分隔横线样式
  header_rule_lwd   <- cfg$figure$header_rule_linewidth %||% 1.0
  header_rule_color <- cfg$figure$header_rule_color     %||% "#555555"
  
  # 是否使用“全局统一横线”（确保无断点）
  header_rule_unified <- cfg$figure$header_rule_unified %||% TRUE
  # 全局横线在整个 body 高度中的相对 y（0=底部, 1=顶部）
  header_rule_y_rel   <- cfg$figure$header_rule_y_rel   %||% 0.935
  
  # 森林图表头与轴标签
  forest_header_label <- cfg$figure$forest_header_label %||% "SMD (95% CI)"
  forest_header_just  <- cfg$figure$forest_header_just  %||% 0.5
  forest_xlab <- cfg$figure$forest_xlab %||% "SMD (95% CI)"
  forest_ylab <- cfg$figure$forest_ylab %||% "Class"
  
  # 森林图背景（圆角）
  # 森林图背景（圆角）
  forest_bg_fill  <- cfg$figure$forest_bg_fill  %||% "#F5F7FB"
  
  # ✅ 透明度规范化：支持 0–1 或 0–100（如 90.0 == 90%）
  forest_bg_alpha_raw <- cfg$figure$forest_bg_alpha %||% 0.6
  forest_bg_alpha <- {
    a <- suppressWarnings(as.numeric(forest_bg_alpha_raw))
    if (!is.finite(a)) 0.6 else {
      if (a > 1) a <- a / 100
      a <- max(0, min(1, a))
      a
    }
  }
  
  forest_bg_r     <- cfg$figure$forest_bg_r     %||% 0.06
  
  # 是否保留 x=0 参考线
  show_zero_vline <- cfg$figure$show_zero_vline %||% FALSE
  
  # 标题与 class 对齐
  title_align_to_class <- cfg$figure$title_align_to_class %||% TRUE
  title_x_nudge        <- cfg$figure$title_x_nudge        %||% 0.0
  # --- 导出 PDF 的开关（默认单图 PDF：开；合订多页 PDF：关） ---
  save_pdf_each <- cfg$figure$save_pdf_each %||% TRUE
  save_pdf_all  <- cfg$figure$save_pdf_all  %||% FALSE
  pdf_device_fun <- if (capabilities("cairo")) grDevices::cairo_pdf else grDevices::pdf
  # --- 排序：允许在 YAML 配置最终呈现顺序（从上到下） ---
  # 支持两种写法：order: {class: [...], stage: [...]} 或 figure$order 下
  class_order <- cfg$order$class %||% (cfg$figure$order$class %||% NULL)
  stage_order <- cfg$order$stage %||% (cfg$figure$order$stage %||% NULL)
  
  ensure_dir(out_dir)
  input_path <- file.path(in_path, in_file)
  message("读取数据：", normalizePath(input_path))
  stopifnot(file.exists(input_path))
  
  dat_raw <- readr::read_csv(input_path, show_col_types = FALSE)
  
  required_cols <- c(
    "Study", "class", "Outcome", "unit_convert", "new_timepoint",
    "Control_group", "Treatment_outcome", "Add_amount_outcome",
    "Csample", "Cmean", "Csd", "Tsample", "Tmean", "Tsd"
  )
  miss_cols <- setdiff(required_cols, names(dat_raw))
  if (length(miss_cols) > 0) stop("数据缺少必要列：", paste(miss_cols, collapse = ", "))
  
  dat <- dat_raw |>
    dplyr::mutate(
      unit_convert = suppressWarnings(as.numeric(unit_convert)),
      unit_convert = ifelse(is.na(unit_convert), 1, unit_convert),
      Cmean = Cmean * unit_convert,
      Tmean = Tmean * unit_convert,
      Csd   = Csd * abs(unit_convert),
      Tsd   = Tsd * abs(unit_convert),
      Outcome = as.character(Outcome),
      class = ifelse(is.na(class) | trimws(class) == "", "Unclassified", as.character(class)),
      new_timepoint = ifelse(is.na(new_timepoint) | trimws(as.character(new_timepoint)) == "",
                             "Unspecified", as.character(new_timepoint))
    ) |>
    dplyr::filter(Outcome %in% outcomes_keep)
  
  
  if (nrow(dat) == 0) stop("筛选后无可用数据（检查 Outcome 是否为 ADG/ADFI/G/F）")
  
  # ✅ 分组计算顺序：Outcome（按 outcomes_keep）→ class（按 YAML）→ new_timepoint（按 YAML）
  groups <- dat |>
    dplyr::distinct(Outcome, new_timepoint, class) |>
    dplyr::mutate(
      .class_sort = factor(class, levels = make_preferred_levels(class, class_order), ordered = TRUE),
      .stage_sort = factor(new_timepoint, levels = make_preferred_levels(new_timepoint, stage_order), ordered = TRUE)
    ) |>
    dplyr::arrange(match(Outcome, outcomes_keep), .class_sort, .stage_sort) |>
    dplyr::select(Outcome, new_timepoint, class)
  
  
  n_groups <- nrow(groups)
  message("开始计算：", n_groups, " 个 Outcome×class 组合 …")
  pb <- utils::txtProgressBar(min = 0, max = n_groups, style = 3)
  
  results <- list()
  for (i in seq_len(n_groups)) {
    g <- groups[i, ]
    df_sub <- dat |>
      dplyr::filter(Outcome == g$Outcome,
                    new_timepoint == g$new_timepoint,
                    class == g$class)
    
    
    res_md  <- safe_metacont(df_sub, sm = "MD",  method_tau = method_tau)
    res_smd <- safe_metacont(df_sub, sm = "SMD", method_tau = method_tau)
    
    P_egger <- safe_egger_p(res_smd$obj)
    
    results[[i]] <- tibble::tibble(
      Outcome      = g$Outcome,
      new_timepoint = g$new_timepoint,     # ✅ 新增
      class        = g$class,
      k            = res_md$k %||% res_smd$k %||% nrow(df_sub),
      # WMD
      WMD      = res_md$TE    %||% NA_real_,
      WMD_lo   = res_md$lower %||% NA_real_,
      WMD_hi   = res_md$upper %||% NA_real_,
      I2_WMD   = res_md$I2    %||% NA_real_,
      # SMD
      SMD      = res_smd$TE    %||% NA_real_,
      SMD_lo   = res_smd$lower %||% NA_real_,
      SMD_hi   = res_smd$upper %||% NA_real_,
      I2_SMD   = res_smd$I2    %||% NA_real_,
      P_Egger  = P_egger,
      method_tau = method_tau
    )
    utils::setTxtProgressBar(pb, i)
  }
  close(pb)
  
  res_df <- dplyr::bind_rows(results) |>
    dplyr::mutate(
      Outcome = factor(Outcome, levels = outcomes_keep, ordered = TRUE),
      class   = as.character(class),
      P_Egger = suppressWarnings(as.numeric(P_Egger)),
      # ✨ 先把 I² 统一为 0–100（百分数）
      I2_WMD = i2_to_pct(I2_WMD),
      I2_SMD = i2_to_pct(I2_SMD),
      
      WMD_ci  = format_ci(WMD, WMD_lo, WMD_hi),
      SMD_ci  = format_ci(SMD, SMD_lo, SMD_hi),
      # ✨ 再格式化文案
      I2_txt  = ifelse(is.na(I2_SMD), "NA", sprintf("%.1f", I2_SMD)),
      P_egger_txt = fmt_p(P_Egger)
    )
  
  out_csv <- file.path(out_dir, paste0("pooled_results_", format(Sys.time(), "%Y%m%d_%H%M%S"), ".csv"))
  readr::write_excel_csv(res_df, out_csv)
  message("已导出结果表：", normalizePath(out_csv))
  
  # -------- 制图函数（离散 y 轴 + 列名可改） --------
  make_text_panel_left <- function(df) {
    # df 需包含：class, new_timepoint, k, WMD_ci, row_id（已经在外层准备）
    # 列宽/对齐：支持 YAML 给 3 或 4 列；不足自动补成 4 列：Class | Time | k | WMD
    left_w <- normalize_vec_len(left_cols_width, 4, fill = 0.5)
    left_j <- normalize_vec_len(left_cols_just,  4, fill = 0.0)
    # 列名：若 YAML 只给 3 个，自动补上 "Time"
    left_h <- left_headers
    if (length(left_h) < 4) left_h <- c(left_h[1], "Time", left_h[-1])
    
    pos <- compute_col_pos(left_w, left_j, pad = col_padding)
    y_levels <- levels(df$row_id)
    y_min <- 0.5
    y_max <- length(y_levels) + 0.6
    
    p <- ggplot(df) +
      geom_text(aes(x = pos$x[1], y = row_id, label = class),         hjust = left_j[1], size = lab_sz) +
      geom_text(aes(x = pos$x[2], y = row_id, label = new_timepoint), hjust = left_j[2], size = lab_sz) +
      geom_text(aes(x = pos$x[3], y = row_id, label = ifelse(is.na(k), "NA", as.character(k))),
                hjust = left_j[3], size = lab_sz) +
      geom_text(aes(x = pos$x[4], y = row_id, label = WMD_ci),        hjust = left_j[4], size = lab_sz) +
      annotate("text", x = pos$x[1], y = Inf, label = left_h[1], hjust = left_j[1], vjust = 1.5, fontface = "bold", size = lab_sz) +
      annotate("text", x = pos$x[2], y = Inf, label = left_h[2], hjust = left_j[2], vjust = 1.5, fontface = "bold", size = lab_sz) +
      annotate("text", x = pos$x[3], y = Inf, label = left_h[3], hjust = left_j[3], vjust = 1.5, fontface = "bold", size = lab_sz) +
      annotate("text", x = pos$x[4], y = Inf, label = left_h[4], hjust = left_j[4], vjust = 1.5, fontface = "bold", size = lab_sz) +
      scale_x_continuous(limits = pos$xlim, breaks = NULL, labels = NULL, expand = expansion(mult = c(0, 0.02))) +
      scale_y_discrete(limits = y_levels, expand = expansion(add = c(0.5, 0.6), mult = c(0, 0))) +
      coord_cartesian(ylim = c(y_min, y_max), clip = "off") +
      labs(x = NULL, y = NULL) +
      theme_void(base_size = fig_base) +
      theme(plot.margin = margin(0,0,0,0))
    
    if (!isTRUE(header_rule_unified)) {
      y_rule <- length(y_levels) + 0.6
      p <- p + annotate("segment",
                        x = pos$xlim[1], xend = pos$xlim[2],
                        y = y_rule,      yend = y_rule,
                        linewidth = header_rule_lwd, colour = header_rule_color)
    }
    p
  }
  
  make_text_panel_right <- function(df) {
    # df 需包含：SMD_ci, I2_txt, P_egger_txt, row_id
    pos <- compute_col_pos(right_cols_width, right_cols_just, pad = col_padding)
    y_levels <- levels(df$row_id)
    y_min <- 0.5
    y_max <- length(y_levels) + 0.6
    
    p <- ggplot(df) +
      geom_text(aes(x = pos$x[1], y = row_id, label = SMD_ci),      hjust = right_cols_just[1], size = lab_sz) +
      geom_text(aes(x = pos$x[2], y = row_id, label = I2_txt),      hjust = right_cols_just[2], size = lab_sz) +
      geom_text(aes(x = pos$x[3], y = row_id, label = P_egger_txt), hjust = right_cols_just[3], size = lab_sz) +
      annotate("text", x = pos$x[1], y = Inf, label = right_headers[1], hjust = right_cols_just[1], vjust = 1.5, fontface = "bold", size = lab_sz) +
      annotate("text", x = pos$x[2], y = Inf, label = right_headers[2], hjust = right_cols_just[2], vjust = 1.5, fontface = "bold", size = lab_sz) +
      annotate("text", x = pos$x[3], y = Inf, label = right_headers[3], hjust = right_cols_just[3], vjust = 1.5, fontface = "bold", size = lab_sz) +
      scale_x_continuous(limits = pos$xlim, breaks = NULL, labels = NULL, expand = expansion(mult = c(0, 0.02))) +
      scale_y_discrete(limits = y_levels, expand = expansion(add = c(0.5, 0.6), mult = c(0, 0))) +
      coord_cartesian(ylim = c(y_min, y_max), clip = "off") +
      labs(x = NULL, y = NULL) +
      theme_void(base_size = fig_base) +
      theme(plot.margin = margin(0,0,0,0))
    
    if (!isTRUE(header_rule_unified)) {
      y_rule <- length(y_levels) + 0.6
      p <- p + annotate("segment",
                        x = pos$xlim[1], xend = pos$xlim[2],
                        y = y_rule,      yend = y_rule,
                        linewidth = header_rule_lwd, colour = header_rule_color)
    }
    p
  }
  
  make_forest_panel_smd <- function(df) {
    # df 需包含：SMD, SMD_lo, SMD_hi, row_id
    rng <- range(c(df$SMD_lo, df$SMD_hi), na.rm = TRUE)
    if (!is.finite(rng[1]) || !is.finite(rng[2]) || rng[1] == rng[2]) {
      rng <- c(-1, 1)
    } else {
      pad <- diff(rng) * 0.08
      rng <- c(rng[1] - pad, rng[2] + pad)
    }
    rng <- range(c(rng, 0))   # 确保包含 0
    
    y_levels <- levels(df$row_id)
    y_min <- 0.5
    y_max <- length(y_levels) + 0.6   # 顶到表头横线所在的上边界
    
    ggplot(df, aes(y = row_id)) +
      { if (isTRUE(show_zero_vline)) geom_vline(xintercept = 0, linetype = 1, linewidth = vline_lwd, color = "grey40") } +
      geom_errorbar(aes(xmin = SMD_lo, xmax = SMD_hi), orientation = "y", width = 0, linewidth = err_lwd) +
      geom_point(aes(x = SMD), size = point_size, color = point_color) +
      scale_x_continuous(limits = rng) +
      # 与左右面板保持完全一致：同样的 levels 与 expand
      scale_y_discrete(limits = y_levels, expand = expansion(add = c(0.5, 0.6), mult = c(0, 0))) +
      # 统一限定同样的可视范围；clip="on" 防越界
      coord_cartesian(xlim = rng, ylim = c(y_min, y_max), clip = "on") +
      # 仅隐藏纵坐标标签（文字），保留纵轴线与刻度
      labs(x = forest_xlab, y = NULL) +
      theme_classic(base_size = fig_base) +
      theme(
        axis.text.y  = element_blank(),  # 不要 y 轴标签文字
        axis.title.y = element_blank(),
        panel.grid   = element_blank(),
        plot.margin  = margin(0,0,0,0)
      )
  }
  
  out_png_list <- list()
  timestamp_tag <- format(Sys.time(), "%Y%m%d_%H%M%S")
  out_pdf_list <- list()   # 记录每个图的 PDF 路径
  all_plots <- list()      # 收集图对象，便于合订多页 PDF
  all_pdf_path <- NULL     # 合订 PDF 的最终路径
  
  
  for (oc in levels(res_df$Outcome)) {
    # 取该 Outcome 的全部 (class, new_timepoint) 汇总行，并按 YAML 指定的顺序排序
    df_oc <- res_df |>
      dplyr::filter(Outcome == oc) |>
      dplyr::mutate(
        .class_sort = factor(class, levels = make_preferred_levels(class, class_order), ordered = TRUE),
        .stage_sort = factor(new_timepoint, levels = make_preferred_levels(new_timepoint, stage_order), ordered = TRUE)
      ) |>
      dplyr::arrange(.class_sort, .stage_sort)
    
    # 仅用于“出图”的过滤：k < 3 的行不画（但 CSV 中仍然保留）
    df_oc <- df_oc |>
      dplyr::filter(!is.na(k) & k >= 3)
    
    # 若该 Outcome 没有任何满足条件的行，则跳过出图
    if (nrow(df_oc) == 0) {
      message("该 Outcome（", oc, "）无 k≥3 的条目，跳过出图。")
      next
    }
    
    # 统一的行标识（每行 = class + timepoint），并用“排序后的先后次序”作为从上到下的显示顺序
    df_oc <- df_oc |>
      dplyr::mutate(row_id_raw = paste0(class, " | ", new_timepoint))
    
    # ggplot 的 y 从下到上，反转 level 让第一行显示在最上方
    row_levels <- rev(unique(df_oc$row_id_raw))
    df_oc$row_id <- factor(df_oc$row_id_raw, levels = row_levels, ordered = TRUE)
    
    left_p   <- make_text_panel_left(df_oc)
    forest_p <- make_forest_panel_smd(df_oc)
    right_p  <- make_text_panel_right(df_oc)
    
    body <- cowplot::plot_grid(
      left_p, forest_p, right_p,
      ncol = 3,
      rel_widths = panel_widths,
      align = "hv", axis = "tblr"
    )
    
    
    # 全局“无断点”分隔横线
    body_g <- cowplot::ggdraw(body)
    if (isTRUE(header_rule_unified)) {
      body_g <- body_g +
        cowplot::draw_line(
          x = c(0, 1),
          y = c(header_rule_y_rel, header_rule_y_rel),
          size = header_rule_lwd,
          color = header_rule_color
        )
    }
    
    # 计算标题 x，与左面板 class 列对齐（左面板有 4 列，Class 在第 1 列）
    sum_pw <- sum(panel_widths)
    left_rel_w <- panel_widths[1] / sum_pw
    # 这里用 4 列配置（get pos）
    pos_left <- compute_col_pos(
      normalize_vec_len(left_cols_width, 4, fill = 0.5),
      normalize_vec_len(left_cols_just,  4, fill = 0.0),
      pad = col_padding
    )
    left_total <- sum(normalize_vec_len(left_cols_width, 4, fill = 0.5))
    # ← 上面这行改成下面这行（R 正确取值）：
    left_class_x_rel_in_panel <- pos_left$x[1] / left_total
    
    title_x <- 0
    if (isTRUE(title_align_to_class)) {
      title_x <- left_rel_w * left_class_x_rel_in_panel + title_x_nudge
      title_x <- min(max(title_x, 0), 1)
    }
    
    # 主标题（模板支持 {OUTCOME}/{METHOD_TAU}，若模板含 {TIMEPOINT} 会被忽略）
    build_title <- function(template, outcome, method_tau) {
      out <- template
      out <- gsub("\\{OUTCOME\\}", outcome, out)
      out <- gsub("\\{METHOD_TAU\\}", method_tau, out)
      out <- gsub("\\{TIMEPOINT\\}", "", out)  # 若模板里有，占位清空
      out
    }
    title_txt <- build_title(title_template, oc, method_tau)
    
    title_g <- cowplot::ggdraw() +
      cowplot::draw_label(title_txt, x = title_x, hjust = 0, fontface = "bold", size = title_sz)
    
    p_final <- cowplot::plot_grid(title_g, body_g, ncol = 1, rel_heights = c(0.08, 1))
    
    safe_oc <- sanitize_filename(oc)
    out_png <- file.path(out_dir, paste0("forest_", safe_oc, "_", timestamp_tag, ".png"))
    ggsave(out_png, p_final, width = fig_w, height = fig_h, dpi = fig_dpi)
    message("已导出森林图（", oc, "）：", normalizePath(out_png))
    out_png_list[[oc]] <- out_png
    # —— 额外导出单图 PDF（可配置） ——
    if (isTRUE(save_pdf_each)) {
      out_pdf <- file.path(out_dir, paste0("forest_", safe_oc, "_", timestamp_tag, ".pdf"))
      ggsave(out_pdf, p_final, width = fig_w, height = fig_h, device = pdf_device_fun)
      message("已导出 PDF（", oc, "）：", normalizePath(out_pdf))
      out_pdf_list[[oc]] <- out_pdf
    }
    
    # —— 收集图对象用于合订多页 PDF ——
    all_plots[[length(all_plots) + 1]] <- p_final
  }
  # —— 合订多页 PDF（可配置） ——
  if (isTRUE(save_pdf_all) && length(all_plots) > 0) {
    all_pdf_path <- file.path(out_dir, paste0("forest_ALL_", timestamp_tag, ".pdf"))
    pdf_device_fun(file = all_pdf_path, width = fig_w, height = fig_h, onefile = TRUE)
    for (p in all_plots) print(p)
    grDevices::dev.off()
    message("已导出合订多页 PDF：", normalizePath(all_pdf_path))
  }
  
  
  invisible(list(
    results_csv     = out_csv,
    forest_pngs     = out_png_list,
    forest_pdfs     = out_pdf_list,
    forest_pdf_all  = all_pdf_path,
    results         = res_df
  ))
}

# 直接运行（按需修改 YAML 文件名/路径）
run_meta_from_yaml(".yaml")
