# ------------------------------------------------------------
# meta_bubble_GF_from_yaml.R (stable)
# 功能：
# - 从 YAML 读取：输入/输出/颜色/大小/顺序等
# - 仅对 Outcome == "G/F" 做分组合并（class × new_timepoint）
# - 合并：Hedges' g (SMD)，随机效应优先；REML 不收敛时自动兜底 DL；仍不稳则用固定效应
# - 预处理：将极小/0 的 SD 抬升到 sd_min（YAML 可配）
# - 导出：CSV（保留全部分组，含 n_total、k 与 plotted），PNG/PDF
# - 作图：仅绘制 n_total > 0 且 k >= min_k 的分组（默认 min_k = 3）
# - 排序：先按 Class 字母顺序，再在同一 Class 内按 TE 由低到高排序
# 依赖：yaml, readr, dplyr, tidyr, stringr, meta, ggplot2, scales, cowplot, purrr, tibble, forcats
# ------------------------------------------------------------

suppressPackageStartupMessages({
  req_pkgs <- c("yaml","readr","dplyr","tidyr","stringr","meta",
                "ggplot2","scales","cowplot","purrr","tibble","forcats")
  to_install <- req_pkgs[!req_pkgs %in% rownames(installed.packages())]
  if (length(to_install) > 0) install.packages(to_install, quiet = TRUE)
  lapply(req_pkgs, require, character.only = TRUE)
})

`%||%` <- function(x, y) if (is.null(x)) y else x

ensure_dir <- function(path) {
  if (!dir.exists(path)) dir.create(path, recursive = TRUE, showWarnings = FALSE)
  invisible(path)
}

# -------- 主函数 --------
run_meta_bubble <- function(config_file = "plot3.yaml") {
  cfg <- yaml::read_yaml(config_file)
  
  # ---- 读入配置 ----
  in_path  <- cfg$input$path  %||% "."
  in_file  <- cfg$input$file  %||% "meta_input.csv"
  out_dir  <- cfg$output$dir  %||% "./out"
  ensure_dir(out_dir)
  
  out_csv  <- file.path(out_dir, cfg$output$summary_csv %||% "GF_meta_summary.csv")
  out_png  <- file.path(out_dir, cfg$output$figure_png  %||% "GF_bubble.png")
  out_pdf  <- file.path(out_dir, cfg$output$figure_pdf  %||% "GF_bubble.pdf")
  
  # 类别与阶段顺序（图例与分面可用；横轴顺序由我们后面自定义排序确定）
  class_levels <- cfg$levels$class %||% c("Probiotics","Antimicrobial peptides","Enzymes",
                                          "Herbal","Plant extracts","Prebiotics")
  tp_levels    <- cfg$levels$new_timepoint %||% c("piglet","growing pig","finishing pig")
  
  # 颜色 & 大小映射
  class_colors <- unlist(cfg$plot$colors$class)
  size_map     <- unlist(cfg$plot$sizes$new_timepoint)
  
  # 设备与主题参数
  width   <- cfg$plot$device$width  %||% 14
  height  <- cfg$plot$device$height %||% 7
  dpi     <- cfg$plot$device$dpi    %||% 300
  base_sz <- cfg$plot$theme$base_size %||% 12
  legend_pos <- cfg$plot$theme$legend_position %||% "right"
  zero_line  <- cfg$plot$guides$hline0 %||% TRUE
  y_limits   <- cfg$plot$limits$y %||% NULL
  x_label    <- cfg$plot$labels$x %||% "Class × Timepoint"
  y_label    <- cfg$plot$labels$y %||% "G/F (SMD)"
  title_txt  <- cfg$plot$labels$title %||% "Combined Effect on G/F by Class × Timepoint"
  subtitle_txt <- cfg$plot$labels$subtitle %||% NULL
  caption_txt  <- cfg$plot$labels$caption %||% NULL
  
  # 作图门槛：每组最少研究条数
  min_k <- cfg$rules$min_k %||% 3
  
  # 预处理：极小 SD 阈值
  sd_min <- cfg$preprocess$sd_min %||% 1e-6
  
  # ---- 读入数据 ----
  dat_path <- file.path(in_path, in_file)
  dat <- readr::read_csv(dat_path, show_col_types = FALSE)
  
  # 必需列检查
  need_cols <- c("Study","class","Outcome","unit_convert","new_timepoint",
                 "Control_group","Treatment_outcome","Add_amount_outcome",
                 "Csample","Cmean","Csd","Tsample","Tmean","Tsd")
  miss <- setdiff(need_cols, names(dat))
  if (length(miss) > 0) stop("输入文件缺少列：", paste(miss, collapse = ", "))
  
  # 因子水平（图例与分面控制；排序稍后单独处理）
  dat <- dat %>%
    mutate(
      class = factor(class, levels = class_levels),
      new_timepoint = factor(new_timepoint, levels = tp_levels)
    )
  
  # 仅保留 G/F
  gf <- dat %>% filter(Outcome == "G/F", !is.na(class), !is.na(new_timepoint))
  if (nrow(gf) == 0) stop("没有符合条件（Outcome == 'G/F'）的数据。")
  
  # 单位换算 + SD 下限
  gf <- gf %>%
    mutate(
      unit_factor = suppressWarnings(as.numeric(unit_convert)),
      unit_factor = ifelse(is.na(unit_factor), 1, unit_factor),
      Cmean = Cmean * unit_factor,
      Tmean = Tmean * unit_factor,
      Csd   = pmax(Csd * unit_factor, sd_min),
      Tsd   = pmax(Tsd * unit_factor, sd_min)
    )
  
  # ---- 合并函数（兜底 + 记录总样本量） ----
  meta_each <- function(d) {
    n_total <- if (all(is.na(d$Csample)) && all(is.na(d$Tsample))) NA_real_ else
      sum(d$Csample, na.rm = TRUE) + sum(d$Tsample, na.rm = TRUE)
    
    d <- d %>% dplyr::filter(
      is.finite(Tsample), is.finite(Csample),
      is.finite(Tmean),   is.finite(Cmean),
      is.finite(Tsd),     is.finite(Csd)
    )
    
    k <- nrow(d)
    if (k == 0) {
      return(tibble::tibble(
        k = 0, n_total = n_total, model = NA_character_,
        TE = NA_real_, seTE = NA_real_, lower = NA_real_, upper = NA_real_,
        pval = NA_real_, I2 = NA_real_, tau2 = NA_real_
      ))
    }
    
    if (k == 1) {
      # 固定效应，HK 与否无意义，这里不传 method.random.ci
      m1 <- tryCatch(
        meta::metacont(
          n.e = d$Tsample, mean.e = d$Tmean, sd.e = d$Tsd,
          n.c = d$Csample, mean.c = d$Cmean, sd.c = d$Csd,
          studlab = d$Study,
          sm = "SMD", method.smd = "Hedges",
          method.tau = "REML", level = 0.95,
          common = TRUE, random = FALSE
        ),
        error = function(e) NULL
      )
      if (is.null(m1)) {
        return(tibble::tibble(
          k = 1, n_total = n_total, model = "single-study-failed",
          TE = NA_real_, seTE = NA_real_, lower = NA_real_, upper = NA_real_,
          pval = NA_real_, I2 = NA_real_, tau2 = NA_real_
        ))
      }
      return(tibble::tibble(
        k = 1, n_total = n_total, model = "single-study",
        TE = as.numeric(m1$TE.fixed), seTE = as.numeric(m1$seTE.fixed),
        lower = as.numeric(m1$lower.fixed), upper = as.numeric(m1$upper.fixed),
        pval = as.numeric(m1$pval.fixed), I2 = NA_real_, tau2 = NA_real_
      ))
    }
    
    fit_try <- function(method.tau = "REML", random = TRUE, common = FALSE) {
      meta::metacont(
        n.e = d$Tsample, mean.e = d$Tmean, sd.e = d$Tsd,
        n.c = d$Csample, mean.c = d$Cmean, sd.c = d$Csd,
        studlab = d$Study,
        sm = "SMD", method.smd = "Hedges",
        method.tau = method.tau, method.random.ci = "HK", level = 0.95,
        random = random, common = common,
        control = list(maxiter = 10000, stepadj = 0.5)
      )
    }
    
    m <- tryCatch(fit_try("REML", random = TRUE,  common = FALSE), error = function(e) NULL); model_tag <- "REML"
    if (is.null(m)) { m <- tryCatch(fit_try("DL",   random = TRUE,  common = FALSE), error = function(e) NULL); model_tag <- "DL" }
    if (is.null(m)) { m <- tryCatch(fit_try("DL",   random = FALSE, common = TRUE ), error = function(e) NULL); model_tag <- "fixed" }
    
    if (is.null(m)) {
      return(tibble::tibble(
        k = k, n_total = n_total, model = "failed",
        TE = NA_real_, seTE = NA_real_, lower = NA_real_, upper = NA_real_,
        pval = NA_real_, I2 = NA_real_, tau2 = NA_real_
      ))
    }
    
    if (identical(model_tag, "fixed")) {
      tibble::tibble(
        k = m$k, n_total = n_total, model = "fixed",
        TE = as.numeric(m$TE.fixed), seTE = as.numeric(m$seTE.fixed),
        lower = as.numeric(m$lower.fixed), upper = as.numeric(m$upper.fixed),
        pval = as.numeric(m$pval.fixed), I2 = NA_real_, tau2 = NA_real_
      )
    } else if (!is.null(m$TE.random) && is.finite(m$TE.random)) {
      tibble::tibble(
        k = m$k, n_total = n_total, model = model_tag,
        TE = as.numeric(m$TE.random), seTE = as.numeric(m$seTE.random),
        lower = as.numeric(m$lower.random), upper = as.numeric(m$upper.random),
        pval = as.numeric(m$pval.random),
        I2 = as.numeric(m$I2), tau2 = as.numeric(m$tau^2)
      )
    } else if (!is.null(m$TE.fixed) && is.finite(m$TE.fixed)) {
      tibble::tibble(
        k = m$k, n_total = n_total, model = "fixed-fallback",
        TE = as.numeric(m$TE.fixed), seTE = as.numeric(m$seTE.fixed),
        lower = as.numeric(m$lower.fixed), upper = as.numeric(m$upper.fixed),
        pval = as.numeric(m$pval.fixed), I2 = NA_real_, tau2 = NA_real_
      )
    } else {
      tibble::tibble(
        k = m$k, n_total = n_total, model = "failed",
        TE = NA_real_, seTE = NA_real_, lower = NA_real_, upper = NA_real_,
        pval = NA_real_, I2 = NA_real_, tau2 = NA_real_
      )
    }
  }
  
  # ---- 分组合并：class × new_timepoint ----
  sum_tbl <- gf %>%
    group_by(class, new_timepoint) %>%
    tidyr::nest() %>%
    mutate(meta = purrr::map(data, meta_each)) %>%
    tidyr::unnest(meta) %>%
    ungroup() %>%
    mutate(combo = paste0(as.character(class), " | ", as.character(new_timepoint)))
  
  # 排序辅助列
  sum_tbl <- sum_tbl %>%
    mutate(
      class_chr = as.character(class),          # Class 的字符版（用于字母顺序）
      TE_ord = ifelse(is.finite(TE), TE, NA_real_)
    )
  
  # ---- 作图资格：n_total > 0 且 k >= min_k ----
  sum_tbl <- sum_tbl %>%
    mutate(plotted = (!is.na(n_total) & n_total > 0) & (!is.na(k) & k >= min_k))
  
  # 写完整 CSV（保留全部分组）
  readr::write_csv(sum_tbl, out_csv)
  
  # ---- 作图数据（仅满足门槛的分组）----
  plot_tbl <- sum_tbl %>% filter(plotted)
  
  # 横轴排序：先按阶段顺序（piglet、growing pig、finishing pig），阶段内按 TE 由低到高
  plot_tbl <- plot_tbl %>%
    arrange(new_timepoint, TE_ord, class_chr)
  
  combo_levels <- plot_tbl$combo
  
  # 同步 x 水平
  sum_tbl  <- sum_tbl  %>% mutate(combo = factor(combo, levels = combo_levels))
  plot_tbl <- plot_tbl %>% mutate(
    class = factor(class, levels = class_levels),
    new_timepoint = factor(new_timepoint, levels = tp_levels),
    combo = factor(combo, levels = combo_levels)
  )
  
  
  # 映射检查
  size_vals  <- size_map[tp_levels];        if (any(is.na(size_vals)))  stop("plot.sizes.new_timepoint 中应包含三个阶段的数值映射。")
  color_vals <- class_colors[class_levels]; if (any(is.na(color_vals))) stop("plot.colors.class 中应包含六类添加剂的颜色映射。")
  
  # ---- 画图（无误差棒，实心圆）----
  p <- ggplot(plot_tbl, aes(x = combo, y = TE)) +
    { if (zero_line) geom_hline(yintercept = 0, linetype = "dashed") else NULL } +
    geom_point(
      aes(color = class, size = new_timepoint),
      shape = 16,                    # 实心圆
      alpha = cfg$plot$geom$alpha %||% 0.9
    ) +
    scale_color_manual(values = color_vals, drop = FALSE) +
    scale_size_manual(values = size_vals, breaks = tp_levels, drop = FALSE) +
    coord_cartesian(ylim = y_limits) +
    labs(
      x = x_label,
      y = y_label,
      title = title_txt,
      subtitle = subtitle_txt,
      caption = caption_txt,
      size = cfg$plot$labels$size_legend %||% "Timepoint",
      color = cfg$plot$labels$color_legend %||% "Class"
    ) +
    theme_minimal(base_size = base_sz) +
    theme(
      legend.position = legend_pos,
      axis.text.x = element_text(
        angle = cfg$plot$theme$xtick_angle %||% 30,
        hjust = cfg$plot$theme$xtick_hjust %||% 1
      ),
      panel.grid.minor = element_blank()
    )
  
  # 保存图形
  ggsave(out_png, p, width = width, height = height, dpi = dpi, bg = "white")
  ggsave(out_pdf, p, width = width, height = height, dpi = dpi, bg = "white")
  message("Done.\n- Summary: ", out_csv, "\n- Figure:  ", out_png, " & ", out_pdf)
}

# ---- 直接运行（如果你用 Rscript 执行本文件）----
if (sys.nframe() == 0) {
  args <- commandArgs(trailingOnly = TRUE)
  cfg_file <- if (length(args) >= 1) args[1] else "plot3.yaml"
  run_meta_bubble(cfg_file)
}
