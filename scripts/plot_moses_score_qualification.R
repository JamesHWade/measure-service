#!/usr/bin/env Rscript
# Plot derived score-agreement aggregates; original measurement arrays stay outside
# this report. Requires jsonlite and ggplot2, with no downloaded data or fonts.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) {
  stop("Usage: Rscript scripts/plot_moses_score_qualification.R OUTPUT_DIR")
}
report_dir <- normalizePath(args[[1]], mustWork = TRUE)
report <- jsonlite::fromJSON(
  file.path(report_dir, "report.json"),
  simplifyVector = FALSE
)
if (!identical(report$profile, "moses-score-qualification/1")) {
  stop("Expected the moses-score-qualification/1 report")
}
profiles <- report$profiles
policies <- report$input_policies
rows <- lapply(profiles, function(profile) {
  do.call(
    rbind,
    lapply(policies, function(policy) {
      counts <- report$aggregates[[profile]][[policy]]
      data.frame(
        profile = profile,
        policy = policy,
        compared = counts$comparable_count,
        matched = counts$within_tolerance_count,
        stringsAsFactors = FALSE
      )
    })
  )
})
data <- do.call(rbind, rows)
if (any(data$compared <= 0) || any(data$matched > data$compared)) {
  stop("Every plotted comparison needs a positive denominator and valid count")
}
data$percent <- 100 * data$matched / data$compared
data$version <- ifelse(
  grepl("1.0.0", data$profile, fixed = TRUE),
  "Published BoTier 1.0.0",
  "BoTier v1.1.0 source"
)
data$inputs <- ifelse(
  data$policy == "raw_resolution_array",
  "Per-analyte arrays",
  "Serialized summaries"
)
data$label <- paste(data$version, data$inputs, sep = "\n")
data$label <- factor(data$label, levels = rev(data$label))
data$count_label <- paste0(
  format(data$matched, big.mark = ",", trim = TRUE),
  " / ",
  format(data$compared, big.mark = ",", trim = TRUE)
)
figure <- ggplot2::ggplot(
  data,
  ggplot2::aes(x = label, y = percent, fill = inputs)
) +
  ggplot2::geom_col(width = 0.57, show.legend = FALSE) +
  ggplot2::geom_text(
    ggplot2::aes(label = count_label),
    y = 103,
    hjust = 0,
    size = 3.5,
    colour = "#203047"
  ) +
  ggplot2::coord_flip() +
  ggplot2::scale_fill_manual(
    values = c(
      "Per-analyte arrays" = "#B85C38",
      "Serialized summaries" = "#25777E"
    )
  ) +
  ggplot2::scale_y_continuous(
    limits = c(0, 147),
    breaks = seq(0, 100, 25),
    labels = function(value) paste0(value, "%"),
    expand = ggplot2::expansion(mult = c(0, 0))
  ) +
  ggplot2::labs(
    title = "Which archived scores can we reproduce?",
    subtitle = paste0(
      format(report$observation_count, big.mark = ",", trim = TRUE),
      " observations · two explicit score rules · native binary64 calculations"
    ),
    x = NULL,
    y = "Agreement with archived scalar scores",
    caption = paste0(
      "Agreement uses the fixed absolute and relative tolerance of 1e-10. Counts show matches / comparable rows.\n",
      "Targeted runs have no summary candidate. Numerical agreement does not establish historical software provenance."
    )
  ) +
  ggplot2::theme_minimal(base_size = 11) +
  ggplot2::theme(
    plot.title = ggplot2::element_text(
      size = 18,
      face = "bold",
      colour = "#203047"
    ),
    plot.subtitle = ggplot2::element_text(
      colour = "#526174",
      margin = ggplot2::margin(b = 18)
    ),
    plot.caption = ggplot2::element_text(
      hjust = 0,
      colour = "#526174",
      margin = ggplot2::margin(t = 18)
    ),
    panel.grid.major.y = ggplot2::element_blank(),
    panel.grid.minor = ggplot2::element_blank(),
    axis.text.y = ggplot2::element_text(colour = "#203047", lineheight = 1.15),
    plot.margin = ggplot2::margin(22, 22, 18, 18)
  )
ggplot2::ggsave(
  file.path(report_dir, "score-agreement.png"),
  figure,
  width = 11,
  height = 6.3,
  dpi = 170
)
ggplot2::ggsave(
  file.path(report_dir, "score-agreement.svg"),
  figure,
  width = 11,
  height = 6.3,
  device = grDevices::svg
)
