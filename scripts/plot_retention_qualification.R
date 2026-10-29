#!/usr/bin/env Rscript
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) {
  stop("Usage: Rscript scripts/plot_retention_qualification.R OUTPUT_DIR")
}
root <- normalizePath(args[[1]], mustWork = TRUE)
report <- jsonlite::fromJSON(
  file.path(root, "report.json"),
  simplifyVector = FALSE
)
if (
  !identical(report$profile, "moses-retention-input-qualification/1") ||
    !isTRUE(report$qualified)
) {
  stop("Figures require a completed numerically qualified retention report")
}
laws <- report$law_comparison$by_law
law_data <- data.frame(
  law = names(laws),
  max_relative_error = vapply(
    laws,
    function(x) x$maximum_relative_error,
    numeric(1)
  )
)
labels <- c(
  linear = "Linear",
  quadratic = "Quadratic",
  `neue-kuss` = "Neue–Kuss"
)
law_data$law <- factor(
  labels[law_data$law],
  levels = c("Linear", "Quadratic", "Neue–Kuss")
)
base_theme <- ggplot2::theme_minimal(base_size = 13) +
  ggplot2::theme(
    plot.title = ggplot2::element_text(
      face = "bold",
      colour = "#203047",
      size = 21
    ),
    plot.subtitle = ggplot2::element_text(
      colour = "#52647C",
      margin = ggplot2::margin(b = 18)
    ),
    plot.caption = ggplot2::element_text(
      hjust = 0,
      colour = "#52647C",
      margin = ggplot2::margin(t = 18)
    ),
    plot.title.position = "plot",
    plot.caption.position = "plot",
    panel.grid.minor = ggplot2::element_blank(),
    plot.margin = ggplot2::margin(24, 30, 20, 24)
  )
law_plot <- ggplot2::ggplot(
  law_data,
  ggplot2::aes(x = law, y = max_relative_error)
) +
  ggplot2::geom_hline(
    yintercept = report$law_comparison$relative_tolerance,
    colour = "#B85C38",
    linetype = "dashed"
  ) +
  ggplot2::geom_point(size = 5, colour = "#167D7F") +
  ggplot2::scale_y_log10(limits = c(1e-16, 3e-12)) +
  ggplot2::labs(
    title = "Source retention laws agree numerically",
    subtitle = paste(
      format(report$law_comparison$reference_value_count, big.mark = ","),
      "comparisons · 500 parameter rows · 21 compositions"
    ),
    x = NULL,
    y = "Maximum relative difference (log scale)",
    caption = "Dashed line: declared relative tolerance 1e-12; no absolute tolerance.\nEach law is assessed conditionally. Original model assignment and physical fit quality remain unknown."
  ) +
  base_theme
cases <- utils::read.csv(
  file.path(root, "retention-cases.csv"),
  check.names = FALSE
)
missing_times <- sum(is.na(cases$retention_time_abs_error_min))
finite_cases <- cases[!is.na(cases$retention_time_abs_error_min), ]
finite_cases$case_label <- gsub("_", " ", finite_cases$case_id, fixed = TRUE)
finite_cases$case_label <- factor(
  finite_cases$case_label,
  levels = rev(finite_cases$case_label)
)
finite_cases$error_ns <- finite_cases$retention_time_abs_error_min * 60 * 1e9
time_plot <- ggplot2::ggplot(
  finite_cases,
  ggplot2::aes(x = case_label, y = error_ns)
) +
  ggplot2::geom_col(fill = "#234D78", width = 0.65) +
  ggplot2::coord_flip() +
  ggplot2::scale_y_continuous(expand = ggplot2::expansion(mult = c(0, 0.12))) +
  ggplot2::labs(
    title = "Ideal retention matches the independent oracle",
    subtitle = paste(
      nrow(cases),
      "status comparisons pass · exact binary64 inputs · 70-digit reference"
    ),
    x = NULL,
    y = "Absolute timing difference (nanoseconds)",
    caption = paste0(
      missing_times,
      " right-censored cases have no event-time error and are excluded from these bars.\nThese are numerical differences, not prediction residuals, physical uncertainty or optimizer performance."
    )
  ) +
  base_theme
save_plot <- function(plot, stem, height) {
  ggplot2::ggsave(
    file.path(root, paste0(stem, ".png")),
    plot,
    width = 11,
    height = height,
    dpi = 180,
    bg = "white"
  )
  ggplot2::ggsave(
    file.path(root, paste0(stem, ".svg")),
    plot,
    width = 11,
    height = height,
    device = grDevices::svg,
    bg = "white"
  )
}
save_plot(law_plot, "retention-law-agreement", 6)
save_plot(time_plot, "retention-timing-agreement", 9)
