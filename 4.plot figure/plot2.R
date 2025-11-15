# meta_bubble_from_yaml.R
# ------------------------------------------------------------
# 功能：
# - 从 YAML 读取参数与路径
# - 对 ADG / ADFI 按 class × new_timepoint 做随机效应合并（SMD, Hedges g）
# - 绘制：x = ADFI_SMD 合并值；y = ADG_SMD 合并值；颜色 = class；大小 = 阶段
# - 导出：ADG/ADFI 合并明细、气泡坐标（含 NA）、气泡图
# - YAML 可调：颜色（class_colors）、尺寸（stage_sizes）、图例位置、是否画零线、x/ylim、pad_ratio 等
# 依赖包：yaml, readr, dplyr, tidyr, stringr, meta, ggplot2, scales, colorspace, cowplot, rlang, tibble, ragg, ggnewscale
# 说明：不再使用明暗表示阶段，stage_lightness 参数将被忽略
# ------------------------------------------------------------

suppressPackageStartupMessages({
  req_pkgs <- c("yaml","readr","dplyr","tidyr","stringr","meta",
                "ggplot2","scales","colorspace","cowplot","rlang","tibble","ragg","ggnewscale")
  to_install <- req_pkgs[!req_pkgs %in% rownames(installed.packages())]
  if (length(to_install) > 0) install.packages(to_install, quiet = TRUE)
  lapply(req_pkgs, require, character.only = TRUE)
})

`%||%` <- function(x, y) if (is.null(x)) y else x
ensure_dir <- function(path) { if (!dir.exists(path)) dir.create(path, recursive = TRUE); invisible(path) }

# （保留函数但本脚本不再用到明暗）
shade_by_stage <- function(base_color, l_factors, clamp = TRUE) {
  stopifnot(is.character(base_color), length(base_color) == 1)
  sapply(names(l_factors), function(tp) {
    amt <- as.numeric(l_factors[[tp]])
    if (isTRUE(clamp)) amt <- min(max(amt, 0), 1)
    colorspace::lighten(base_color, amount = amt)
  }, USE.NAMES = TRUE)
}

run_meta_bubble <- function(config_file = "plot2.yaml") {
  cfg <- yaml::read_yaml(config_file)
  
  # ---- 基础配置 ----
  in_path   <- cfg$input$path     %||% "."
  in_file   <- cfg$input$file     %||% "meta_input.csv"
  out_path  <- cfg$output$path    %||% "./output"
  out_stub  <- cfg$output$stub    %||% "bubble"
  width     <- cfg$plot$width     %||% 8
  height    <- cfg$plot$height    %||% 6
  dpi       <- cfg$plot$dpi       %||% 300
  export_svg<- cfg$plot$export_svg %||% FALSE
  export_pdf <- cfg$plot$export_pdf %||% TRUE
  pdf_family <- cfg$plot$pdf_family %||% NULL
  ensure_dir(out_path)
  
  # ---- 过滤与顺序 ----
  class_keep <- cfg$filter$class_keep %||% c("Probiotics","Antimicrobial peptides","Enzymes",
                                             "Herbal","Plant extracts","Prebiotics")
  stage_keep <- cfg$filter$stage_keep %||% c("piglet","growing pig","finishing pig")
  
  # ---- 调色板 ----
  class_colors <- cfg$palette$class_colors %||% rlang::set_names(
    c("#1f77b4","#ff7f0e","#2ca02c","#d62728","#9467bd","#8c564b"), class_keep)
  # 不再用明暗表现阶段，stage_lightness 如存在将被忽略
  stage_lightness <- cfg$palette$stage_lightness %||% c("piglet"=0.85,"growing pig"=0.70,"finishing pig"=0.55)
  
  # ---- 绘图参数 ----
  size_range <- cfg$plot$size_range %||% c(3,16)
  # 读取/默认
  stage_sizes <- cfg$plot$stage_sizes %||% c(
    "piglet" = size_range[1],
    "growing pig" = mean(size_range),
    "finishing pig" = size_range[2]
  )
  
  # ★ 关键：从 YAML 读入的是 list，这里统一转为“命名的数值向量”
  if (is.list(stage_sizes)) stage_sizes <- unlist(stage_sizes, use.names = TRUE)
  stage_sizes <- setNames(as.numeric(stage_sizes), names(stage_sizes))
  
  # 对齐顺序并校验
  stage_sizes <- stage_sizes[stage_keep]
  if (any(is.na(stage_sizes))) {
    stop("plot$stage_sizes 必须包含并命名为：", paste(stage_keep, collapse = ", "))
  }
  
  # 对齐并校验
  stage_sizes <- stage_sizes[stage_keep]
  if (any(is.na(stage_sizes))) stop("plot$stage_sizes 必须包含并命名为：", paste(stage_keep, collapse = ", "))
  stroke     <- cfg$plot$stroke     %||% 0.3
  alpha_pt   <- cfg$plot$alpha      %||% 0.9
  legend_pos <- cfg$plot$legend_position %||% "right"
  grid_minor <- cfg$plot$grid_minor %||% FALSE
  xlab       <- cfg$plot$xlab %||% "ADFI (SMD, random effects)"
  ylab       <- cfg$plot$ylab %||% "ADG (SMD, random effects)"
  title_txt  <- cfg$plot$title %||% "ADG vs ADFI (SMD) by Class × Stage"
  subtitle   <- cfg$plot$subtitle %||% NULL
  caption    <- cfg$plot$caption %||% NULL
  zero_lines <- cfg$plot$zero_lines %||% TRUE
  
  # 轴范围
  xlim_cfg   <- cfg$plot$xlim %||% NULL
  ylim_cfg   <- cfg$plot$ylim %||% NULL
  pad_ratio  <- cfg$plot$pad_ratio %||% 0.05
  
  # ---- 统计方法 ----
  method_tau <- cfg$meta$method_tau %||% "REML"
  hakn       <- cfg$meta$hakn       %||% TRUE
  
  # ---- 读数据 ----
  data_file <- normalizePath(file.path(in_path, in_file), mustWork = TRUE)
  df <- readr::read_csv(data_file, show_col_types = FALSE) |>
    dplyr::mutate(
      class = as.character(class),
      Outcome = as.character(Outcome),
      new_timepoint = as.character(new_timepoint)
    ) |>
    dplyr::filter(class %in% class_keep, new_timepoint %in% stage_keep, Outcome %in% c("ADG","ADFI")) |>
    dplyr::mutate(
      class = factor(class, levels = class_keep),
      new_timepoint = factor(new_timepoint, levels = stage_keep)
    )
  
  # ---- 合并 ----
  meta_by_group <- function(dat, outcome_label) {
    sub <- dat |> dplyr::filter(Outcome == outcome_label)
    if (nrow(sub) == 0) return(tibble::tibble())
    sub |>
      dplyr::group_by(class, new_timepoint) |>
      dplyr::group_modify(~{
        dd <- .x
        if (nrow(dd) < 1) return(tibble::tibble())
        m <- tryCatch(
          meta::metacont(
            n.e = Tsample, mean.e = Tmean, sd.e = Tsd,
            n.c = Csample, mean.c = Cmean, sd.c = Csd,
            data = dd, sm = "SMD", method.tau = method_tau, hakn = hakn, studlab = dd$Study
          ),
          error = function(e) NULL
        )
        if (is.null(m)) return(tibble::tibble(
          k = nrow(dd), TE = NA_real_, seTE = NA_real_, lower = NA_real_, upper = NA_real_,
          tau2 = NA_real_, I2 = NA_real_, Q = NA_real_, pval = NA_real_,
          N_total = sum(dd$Csample + dd$Tsample, na.rm = TRUE)
        ))
        tibble::tibble(
          k = length(m$TE),
          TE = as.numeric(m$TE.random),
          seTE = as.numeric(m$seTE.random),
          lower = as.numeric(m$lower.random),
          upper = as.numeric(m$upper.random),
          tau2 = as.numeric(m$tau2),
          I2 = as.numeric(m$I2),
          Q = as.numeric(m$Q),
          pval = as.numeric(m$pval.random),
          N_total = sum(dd$Csample + dd$Tsample, na.rm = TRUE)
        )
      }) |>
      dplyr::ungroup() |>
      dplyr::rename(stage = new_timepoint) |>
      dplyr::mutate(Outcome = outcome_label)
  }
  
  res_ADG  <- meta_by_group(df, "ADG")
  res_ADFI <- meta_by_group(df, "ADFI")
  
  # ---- 对齐（完整表导出；非 NA/非 0 子集作图）----
  merged_full <- dplyr::full_join(
    res_ADG  |> dplyr::select(class, stage, ADG_SMD = TE, ADG_low = lower, ADG_up = upper,
                              ADG_k = k, ADG_I2 = I2, ADG_N = N_total),
    res_ADFI |> dplyr::select(class, stage, ADFI_SMD = TE, ADFI_low = lower, ADFI_up = upper,
                              ADFI_k = k, ADFI_I2 = I2, ADFI_N = N_total),
    by = c("class","stage")
  )
  # 不再构造 N_bubble（尺寸不再使用样本量）
  
  # ---- 写出 CSV（保留 NA 行）----
  readr::write_csv(res_ADG,  file.path(out_path, paste0(out_stub, "_ADG_meta_detail.csv")))
  readr::write_csv(res_ADFI, file.path(out_path, paste0(out_stub, "_ADFI_meta_detail.csv")))
  readr::write_csv(merged_full, file.path(out_path, paste0(out_stub, "_bubble_points.csv")))
  
  # ---- 颜色映射（颜色只按 class，不随阶段变化）----
  missing_cols <- setdiff(levels(df$class), names(class_colors))
  if (length(missing_cols) > 0) {
    stop("在 YAML 的 palette$class_colors 中缺少以下 class 基色：", paste(missing_cols, collapse = ", "))
  }
  
  class_stage_colors <- tibble::tibble(
    class = rep(names(class_colors), each = length(stage_keep)),
    stage = rep(stage_keep, times = length(class_colors)),
    color = rep(unname(class_colors), each = length(stage_keep))
  )
  
  merged_full <- merged_full |> dplyr::left_join(class_stage_colors, by = c("class","stage"))
  
  # —— 作图数据（去除 NA/0）——
  merged_plot <- merged_full |>
    dplyr::filter(!is.na(ADG_SMD), !is.na(ADFI_SMD), ADG_SMD != 0, ADFI_SMD != 0)
  if (nrow(merged_plot) == 0) {
    warning("没有可绘制的数据点。请检查筛选条件或原始数据。")
    return(invisible(NULL))
  }
  
  # ---- 轴范围（优先 YAML；否则自动加边距）----
  xr <- range(merged_plot$ADFI_SMD, na.rm = TRUE)
  yr <- range(merged_plot$ADG_SMD,  na.rm = TRUE)
  if (!is.finite(diff(xr)) || diff(xr) == 0) {
    bx <- max(1, max(abs(xr), na.rm = TRUE)); xr <- c(xr[1] - bx * pad_ratio, xr[2] + bx * pad_ratio)
  }
  if (!is.finite(diff(yr)) || diff(yr) == 0) {
    by <- max(1, max(abs(yr), na.rm = TRUE)); yr <- c(yr[1] - by * pad_ratio, yr[2] + by * pad_ratio)
  }
  x_pad <- diff(xr) * pad_ratio; y_pad <- diff(yr) * pad_ratio
  xlim_final <- if (is.null(xlim_cfg)) c(xr[1] - x_pad, xr[2] + x_pad) else xlim_cfg
  ylim_final <- if (is.null(ylim_cfg)) c(yr[1] - y_pad, yr[2] + y_pad) else ylim_cfg
  
  legend_anchor_x <- mean(xlim_final); legend_anchor_y <- mean(ylim_final)
  
  # ---- 作图 ----
  p <- ggplot(merged_plot, aes(x = ADFI_SMD, y = ADG_SMD)) +
    { if (isTRUE(zero_lines)) list(
      geom_vline(xintercept = 0, linetype = 2, linewidth = 0.3, alpha = 0.6),
      geom_hline(yintercept = 0, linetype = 2, linewidth = 0.3, alpha = 0.6)
    ) } +
    # 主层：颜色=class，大小=stage
    geom_point(
      aes(size = stage, fill = color, colour = color),
      alpha = alpha_pt, stroke = stroke, shape = 21
    ) +
    scale_fill_identity(guide = "none") +
    scale_colour_identity(guide = "none") +
    # 分隔 fill 标度，避免后续 Class 图例覆盖主层颜色
    ggnewscale::new_scale_fill() +
    
    # ---------- Class 图例 ----------
  geom_point(
    data = tibble::tibble(class = levels(df$class),
                          x = legend_anchor_x, y = legend_anchor_y),
    aes(x = x, y = y, fill = class),
    size = 0, alpha = 0, shape = 21, inherit.aes = FALSE, show.legend = TRUE
  ) +
    scale_fill_manual(
      name   = "Class",
      breaks = levels(df$class),
      values = unname(class_colors[levels(df$class)]),
      guide  = guide_legend(override.aes = list(size = 5, alpha = 1, shape = 21), order = 1)
    ) +
    
    # ---------- Stage（仅用大小表示） ----------
  scale_size_manual(
    name   = "Stage",
    values = stage_sizes,
    breaks = stage_keep,
    guide  = guide_legend(order = 2, override.aes = list(fill = "grey75", colour = "grey50", shape = 21))
  ) +
    
    labs(title = title_txt, subtitle = subtitle, caption = caption, x = xlab, y = ylab) +
    coord_cartesian(xlim = xlim_final, ylim = ylim_final, expand = FALSE) +
    theme_minimal(base_size = 12) +
    theme(
      legend.position = legend_pos,
      panel.grid.minor = if (grid_minor) element_line(color = "grey85", linewidth = 0.2) else element_blank()
    )
  
  # ---- 导出 ----
  png_file <- file.path(out_path, paste0(out_stub, "_bubble.png"))
  ggsave(png_file, p, width = width, height = height, dpi = dpi, bg = "white")
  
  if (isTRUE(export_svg)) {
    svg_file <- file.path(out_path, paste0(out_stub, "_bubble.svg"))
    ggsave(svg_file, p, width = width, height = height, bg = "white", device = ragg::agg_svg)
  }
  
  if (isTRUE(export_pdf)) {
    pdf_file <- file.path(out_path, paste0(out_stub, "_bubble.pdf"))
    if (isTRUE(capabilities("cairo"))) {
      ggsave(
        pdf_file, p, width = width, height = height, bg = "white",
        device = function(filename, width, height, ...)
          grDevices::cairo_pdf(filename = filename, width = width, height = height,
                               family = if (is.null(pdf_family)) "" else pdf_family)
      )
    } else {
      ggsave(
        pdf_file, p, width = width, height = height, bg = "white",
        device = "pdf",
        useDingbats = FALSE,
        family = if (is.null(pdf_family)) "" else pdf_family
      )
    }
  }
  
  message("已输出：",
          "\n- 合并明细（ADG）：", file.path(out_path, paste0(out_stub, "_ADG_meta_detail.csv")),
          "\n- 合并明细（ADFI）：", file.path(out_path, paste0(out_stub, "_ADFI_meta_detail.csv")),
          "\n- 气泡坐标：", file.path(out_path, paste0(out_stub, "_bubble_points.csv")),
          "\n- 氣泡圖（PNG）：", png_file,
          if (isTRUE(export_svg)) paste0("\n- 氣泡圖（SVG）：", file.path(out_path, paste0(out_stub, "_bubble.svg"))) else "",
          if (isTRUE(export_pdf)) paste0("\n- 氣泡圖（PDF）：", file.path(out_path, paste0(out_stub, "_bubble.pdf"))) else ""
  )
  
  invisible(list(
    data = df,
    res_ADG = res_ADG,
    res_ADFI = res_ADFI,
    merged_full = merged_full,
    merged_plot = merged_plot,
    plot = p
  ))
}

# 直接运行（若你使用 Rscript 执行，可在同目录放置 plot2.yaml）
run_meta_bubble("plot2.yaml")
