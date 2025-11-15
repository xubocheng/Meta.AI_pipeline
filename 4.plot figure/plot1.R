suppressPackageStartupMessages({
  req_pkgs <- c("yaml","readr","dplyr","tidyr","stringr","ggplot2","scales","rlang","purrr")
  to_install <- req_pkgs[!req_pkgs %in% rownames(installed.packages())]
  if (length(to_install) > 0) install.packages(to_install, quiet = TRUE)
  lapply(req_pkgs, require, character.only = TRUE)
})

`%||%` <- function(x, y) if (is.null(x)) y else x
ensure_dir <- function(path) if (!dir.exists(path)) dir.create(path, recursive = TRUE, showWarnings = FALSE)

# ---- 计算 Hedges' g 及其方差（每一行/比较） ----
compute_smd_row <- function(Tmean, Cmean, Tsd, Csd, Tsample, Csample) {
  N_t <- Tsample; N_c <- Csample
  sp  <- sqrt(((N_t - 1) * (Tsd^2) + (N_c - 1) * (Csd^2)) / (N_t + N_c - 2))
  d   <- (Tmean - Cmean) / sp
  J   <- 1 - 3 / (4 * (N_t + N_c) - 9)                 # 小样本校正
  g   <- J * d
  v_g <- J^2 * ((N_t + N_c) / (N_t * N_c) + (d^2) / (2 * (N_t + N_c - 2)))
  tibble(g = g, v = v_g, n_total = N_t + N_c)
}

# ---- 在 Study-class-Outcome 内合并（若同一研究有多臂/多行）固定效应 ----
combine_within_study <- function(df) {
  # df 需包含 g, v, n_total
  w <- 1 / df$v
  g_bar <- sum(w * df$g, na.rm = TRUE) / sum(w, na.rm = TRUE)
  v_bar <- 1 / sum(w, na.rm = TRUE)
  n_bar <- mean(df$n_total, na.rm = TRUE)  # 用于气泡大小的代表样本量（可在 YAML 里改策略）
  tibble(g = g_bar, v = v_bar, n_total = n_bar)
}

# ---- 读取 YAML 配置 ----
args <- commandArgs(trailingOnly = TRUE)
cfg_file <- if (length(args) >= 1) args[1] else "plot.yaml"
cfg <- yaml::read_yaml(cfg_file)

# ================ YAML 配置字段说明（简要） ================
# input:
#   path: "./data"                 # 输入数据文件夹
#   file: "meta_input.csv"         # 输入 CSV 文件名
# filter:
#   outcomes: ["ADG","ADFI","G/F"] # 需要计算的结局；至少包含 ADG 与 ADFI
# plot:
#   x_outcome: "ADFI"              # X 轴对应的结局
#   y_outcome: "ADG"               # Y 轴对应的结局
#   class_palette:                 # 颜色映射（class -> 颜色），未覆盖到的会自动分配
#     Herb: "#1f77b4"
#     Peptide: "#ff7f0e"
#   alpha: 0.85                    # 气泡透明度
#   size_range: [3, 16]            # 气泡像素半径范围（连续映射的下/上限）
#   stroke_color: "#333333"        # 气泡描边颜色
#   stroke_width: 0.3              # 气泡描边线宽
#   shape: 21                      # 点形状（21/22/23...支持填充+描边）
#   show_labels: false             # 是否显示 Study 文本标签
#   label_field: "Study"           # 作为标注使用的列
#   label_size: 3.2                # 标注字号
#   label_nudge_x: 0.00            # 标注 X 偏移
#   label_nudge_y: 0.00            # 标注 Y 偏移
#   x_limits: [-2, 2]              # X 轴范围（可留空自动）
#   y_limits: [-2, 2]              # Y 轴范围（可留空自动）
#   add_zero_lines: true           # 是否绘制 x=0 / y=0 参考线
#   theme_base_size: 12            # 主题基础字号
#   legend_position: "right"       # 图例位置（"right","bottom","none"...）
# output:
#   path: "./out"                  # 输出文件夹
#   filename: "bubble_ADG_vs_ADFI.png"  # 输出文件名
#   width: 180                     # 宽度（mm）
#   height: 140                    # 高度（mm）
#   dpi: 300                       # 分辨率
# advanced:
#   bubble_size_metric: "mean"     # 计算两结局样本量为气泡大小：mean / min / max / x_only / y_only
# =========================================================

# ---- 读入数据 ----
in_path <- cfg$input$path %||% "."
in_file <- cfg$input$file %||% "meta_input.csv"
dat <- readr::read_csv(file.path(in_path, in_file), show_col_types = FALSE)

# ---- 仅保留需要的结局 ----
keep_outcomes <- cfg$filter$outcomes %||% c("ADG","ADFI","G/F")
dat <- dat %>%
  filter(.data$Outcome %in% keep_outcomes)

# ---- 逐行计算 SMD（Hedges' g）与方差 ----
es_rows <- dat %>%
  mutate(across(c(Csd, Tsd), ~ ifelse(. <= 0, NA_real_, .))) %>%  # 防御性处理
  mutate(tmp = pmap(list(Tmean, Cmean, Tsd, Csd, Tsample, Csample),
                    ~ compute_smd_row(..1, ..2, ..3, ..4, ..5, ..6))) %>%
  tidyr::unnest(tmp)

# ---- 在 Study + class + Outcome 内做固定效应合并，得到“每个研究-每个结局-每个分类”的单一效应量 ----
es_study <- es_rows %>%
  group_by(Study, class, Outcome) %>%
  reframe(combine_within_study(pick(g, v, n_total))) %>%
  ungroup()

# ---- 将 X/Y 两个结局（默认 ADFI / ADG）拼成宽表，用于散点气泡图 ----
x_out <- cfg$plot$x_outcome %||% "ADFI"
y_out <- cfg$plot$y_outcome %||% "ADG"

wide_xy <- es_study %>%
  select(Study, class, Outcome, g, v, n_total) %>%
  pivot_wider(names_from = Outcome,
              values_from = c(g, v, n_total),
              names_sep = "__") %>%
  # 仅保留同时具备 X、Y 两个结局的研究-分类组合
  filter(!is.na(.data[[paste0("g__", x_out)]]) & !is.na(.data[[paste0("g__", y_out)]]))

# ---- 决定气泡大小所用的样本量（可在 YAML 中挑选策略）----
size_metric <- (cfg$advanced$bubble_size_metric %||% "mean") %>% tolower()
n_x <- wide_xy[[paste0("n_total__", x_out)]]
n_y <- wide_xy[[paste0("n_total__", y_out)]]
bubble_n <- dplyr::case_when(
  size_metric == "min"    ~ pmin(n_x, n_y, na.rm = TRUE),
  size_metric == "max"    ~ pmax(n_x, n_y, na.rm = TRUE),
  size_metric == "x_only" ~ n_x,
  size_metric == "y_only" ~ n_y,
  TRUE                    ~ (n_x + n_y) / 2  # default mean
)
wide_xy <- wide_xy %>% mutate(bubble_n = bubble_n)

# ---- 颜色映射（class -> color），YAML 中没覆盖的 class 自动分配 ----
class_levels <- sort(unique(wide_xy$class))
manual_palette <- cfg$plot$class_palette %||% list()
manual_keys <- names(manual_palette)
# 将 list 转为命名向量
manual_vec <- if (length(manual_keys)) {
  v <- unlist(manual_palette, use.names = TRUE)
  names(v) <- manual_keys
  v
} else c()

# 自动分配未覆盖的 class 颜色
auto_needed <- setdiff(class_levels, names(manual_vec))
if (length(auto_needed) > 0) {
  # 使用 hue 调色自动分配
  auto_cols <- scales::hue_pal()(length(auto_needed))
  names(auto_cols) <- auto_needed
  palette_vec <- c(manual_vec, auto_cols)
} else {
  palette_vec <- manual_vec
}

# ---- 取图形参数 ----
alpha_val      <- cfg$plot$alpha %||% 0.85
size_range     <- cfg$plot$size_range %||% c(3, 16)
stroke_color   <- cfg$plot$stroke_color %||% "#333333"
stroke_width   <- cfg$plot$stroke_width %||% 0.3
shape_val      <- cfg$plot$shape %||% 21
show_labels    <- cfg$plot$show_labels %||% FALSE
label_field    <- cfg$plot$label_field %||% "Study"
label_size     <- cfg$plot$label_size %||% 3.2
label_nx       <- cfg$plot$label_nudge_x %||% 0
label_ny       <- cfg$plot$label_nudge_y %||% 0
x_limits       <- cfg$plot$x_limits %||% NULL
y_limits       <- cfg$plot$y_limits %||% NULL
add_zero_lines <- cfg$plot$add_zero_lines %||% TRUE
theme_base     <- cfg$plot$theme_base_size %||% 12
legend_pos     <- cfg$plot$legend_position %||% "right"
legend_class_pt_size <- cfg$plot$legend$class_point_size %||% 6
legend_key_size_pt   <- cfg$plot$legend$key_size_pt %||% 12
legend_text_size     <- cfg$plot$legend$text_size %||% NA


# ---- 构造数据框以供 ggplot ----
plot_df <- wide_xy %>%
  transmute(
    Study = .data$Study,
    class = .data$class,
    x = .data[[paste0("g__", x_out)]],
    y = .data[[paste0("g__", y_out)]],
    bubble_n = bubble_n,
    label = .data[[label_field]] %||% .data$Study
  )

# ---- 画图 ----
p <- ggplot(plot_df, aes(x = x, y = y)) +
  { if (add_zero_lines) geom_hline(yintercept = 0, linetype = "dashed", linewidth = 0.35, alpha = 0.7) } +
  { if (add_zero_lines) geom_vline(xintercept = 0, linetype = "dashed", linewidth = 0.35, alpha = 0.7) } +
  geom_point(aes(fill = class, size = bubble_n), shape = shape_val, color = stroke_color,
             alpha = alpha_val, stroke = stroke_width) +
  scale_fill_manual(values = palette_vec, drop = FALSE) +
  scale_size_continuous(range = size_range, name = "Sample size") +
  labs(
    x = paste0(x_out, " SMD (Hedges' g)"),
    y = paste0(y_out, " SMD (Hedges' g)"),
    fill = "Additive class"
  ) +
  theme_minimal(base_size = theme_base) +
  theme(
    legend.position = legend_pos,
    panel.grid.minor = element_blank()
  )

if (!is.null(x_limits)) p <- p + coord_cartesian(xlim = x_limits, ylim = y_limits %||% NULL)
if (!is.null(y_limits) && is.null(x_limits)) p <- p + coord_cartesian(ylim = y_limits)

if (isTRUE(show_labels)) {
  p <- p + ggrepel::geom_text_repel(
    aes(label = label),
    size = label_size, max.overlaps = 1000,
    nudge_x = label_nx, nudge_y = label_ny, segment.size = 0.2, alpha = 0.9
  )
}

p <- p +
  guides(
    # 仅调整“class（添加剂）”这一列图例里点的大小
    fill = guide_legend(override.aes = list(size = legend_class_pt_size))
    # 如需同时调大小图例，可加：size = guide_legend(override.aes = list(size = legend_class_pt_size))
  ) +
  theme(
    legend.position = legend_pos,
    legend.key.size = grid::unit(legend_key_size_pt, "pt"),
    legend.text = if (is.na(legend_text_size)) element_text() else element_text(size = legend_text_size)
  )

# ---- 输出保存 ----
out_path <- cfg$output$path %||% "./out"
out_file <- cfg$output$filename %||% paste0("bubble_", y_out, "_vs_", x_out, ".png")
ensure_dir(out_path)

# ① 导出“逐行”效应量（每一条原始比较的 Hedges' g）
readr::write_csv(es_rows %>%
                   select(Study, class, Outcome, Csample, Tsample, g, v, n_total),
                 file.path(out_path, "01_per_row_SMD.csv"))

# ② 导出“研究内合并后”的效应量（Study-class-Outcome 已用固定效应合成）
readr::write_csv(es_study %>%
                   select(Study, class, Outcome, g, v, n_total),
                 file.path(out_path, "02_per_study_FE_combined_SMD.csv"))

# ③ 导出用于作图的宽表（含 X/Y 轴 SMD 与用于气泡大小的样本量）
readr::write_csv(wide_xy %>%
                   mutate(bubble_size_metric = size_metric) %>%
                   select(Study, class,
                          starts_with(paste0("g__", x_out)), starts_with(paste0("v__", x_out)),
                          starts_with(paste0("n_total__", x_out)),
                          starts_with(paste0("g__", y_out)), starts_with(paste0("v__", y_out)),
                          starts_with(paste0("n_total__", y_out)),
                          bubble_n, bubble_size_metric),
                 file.path(out_path, paste0("03_plot_wide_", y_out, "_vs_", x_out, ".csv")))

# ④ 保存 PNG
ggsave(filename = file.path(out_path, out_file),
       plot = p,
       width = (cfg$output$width %||% 180) / 25.4,   # mm -> inch
       height = (cfg$output$height %||% 140) / 25.4, # mm -> inch
       dpi = cfg$output$dpi %||% 300, bg = "white")

# ⑤ 额外导出 PDF（矢量图）
out_pdf_file <- cfg$output$pdf_filename %||% sub("\\.[^.]+$", ".pdf", out_file)

use_cairo <- isTRUE(capabilities("cairo"))

if (use_cairo) {
  # cairo_pdf 不接受 useDingbats 参数
  ggsave(filename = file.path(out_path, out_pdf_file),
         plot = p,
         width = (cfg$output$width %||% 180) / 25.4,
         height = (cfg$output$height %||% 140) / 25.4,
         bg = "white",
         device = grDevices::cairo_pdf)
} else {
  # 基础 pdf 设备可用 useDingbats=FALSE，避免 Dingbats 字体问题
  ggsave(filename = file.path(out_path, out_pdf_file),
         plot = p,
         width = (cfg$output$width %||% 180) / 25.4,
         height = (cfg$output$height %||% 140) / 25.4,
         bg = "white",
         device = "pdf",
         useDingbats = FALSE)
}


message("Done. Saved figure to: ", file.path(out_path, out_file), " and ", file.path(out_path, out_pdf_file))
message("Also wrote CSVs to: ", out_path)


