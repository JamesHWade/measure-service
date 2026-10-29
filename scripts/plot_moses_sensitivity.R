#!/usr/bin/env Rscript
# Figures from derived sensitivity tables only; requires ggplot2 and jsonlite.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) {
  stop("Usage: Rscript scripts/plot_moses_sensitivity.R OUTPUT_DIR")
}
root <- normalizePath(args[[1]], mustWork = TRUE)
report <- jsonlite::fromJSON(
  file.path(root, "report.json"),
  simplifyVector = FALSE
)
if (!identical(report$profile, "moses-trajectory-sensitivity/1")) {
  stop("Expected moses-trajectory-sensitivity/1")
}
curves <- utils::read.csv(
  file.path(root, "success-trajectories.csv"),
  check.names = FALSE
)
keys <- c(
  "archived_score_at_least_two",
  "published_1.0_raw_resolution_array",
  "raw_analytical_thresholds",
  "raw_all_objective_thresholds"
)
labels <- c(
  "Archived score >= 2",
  "Published array score >= 2",
  "Raw count + resolution",
  "Raw count + resolution + runtime"
)
colours <- stats::setNames(
  c("#234D78", "#B85C38", "#167D7F", "#8A7A99"),
  labels
)
settings_label <- function(item) {
  s <- item$scientific_settings
  paste0(
    s$num_segments,
    " seg · ",
    s$surrogate_model,
    " · ",
    s$acquisition_function,
    "\n",
    s$objective_type,
    " · ",
    s$seed_strategy,
    " ",
    s$seed_budget,
    " · ",
    if (isTRUE(s$multi_fidelity)) "MF" else "SF",
    "\n",
    if (isTRUE(s$segment_based)) "segment time" else "absolute time",
    " · ",
    if (isTRUE(s$advanced_peak_detection)) "advanced flag" else "basic flag"
  )
}
configs <- Filter(
  function(item) identical(item$record_kind, "simulated-benchmark"),
  report$configurations
)
if (!length(configs)) {
  stop("No simulated configurations in the sensitivity report")
}
ids <- vapply(configs, `[[`, character(1), "configuration_id")
config_labels <- paste0(
  sprintf("%02d", seq_along(configs)),
  " · ",
  vapply(configs, settings_label, character(1))
)
data <- curves[curves$series %in% keys & curves$configuration_id %in% ids, ]
data$rule <- factor(data$series, levels = keys, labels = labels)
data$configuration <- factor(
  data$configuration_id,
  levels = ids,
  labels = config_labels
)
atlas <- ggplot2::ggplot(
  data,
  ggplot2::aes(x = high_fidelity_budget, y = success_fraction, colour = rule)
) +
  ggplot2::geom_ribbon(
    ggplot2::aes(
      ymin = success_fraction,
      ymax = success_fraction_upper_bound,
      fill = rule
    ),
    alpha = 0.08,
    colour = NA,
    show.legend = FALSE
  ) +
  ggplot2::geom_step(linewidth = 0.48, alpha = 0.9) +
  ggplot2::facet_wrap(ggplot2::vars(configuration), ncol = 4) +
  ggplot2::scale_colour_manual(values = colours, drop = FALSE) +
  ggplot2::scale_fill_manual(values = colours, drop = FALSE) +
  ggplot2::scale_y_continuous(limits = c(0, 1), labels = function(x) {
    paste0(100 * x, "%")
  }) +
  ggplot2::labs(
    title = "The same histories, different success rules",
    subtitle = "All simulated configurations · full 50-mixture denominators · initial-design runs count",
    x = "High-fidelity evaluations",
    y = "Confirmed campaign success fraction",
    colour = NULL,
    caption = paste0(
      "Shading shows unknown-outcome upper bounds, not confidence intervals. Low-fidelity positives never confirm success.\n",
      "Four rules are plotted for readability; all seven are retained in the tables. Labels are stored declarations, not established initialization phases.\n",
      "This does not rerun optimization or validate signal processing."
    )
  ) +
  ggplot2::theme_minimal(base_size = 10) +
  ggplot2::theme(
    plot.title = ggplot2::element_text(
      size = 20,
      face = "bold",
      colour = "#203047"
    ),
    plot.subtitle = ggplot2::element_text(
      colour = "#526174",
      margin = ggplot2::margin(b = 14)
    ),
    strip.text = ggplot2::element_text(size = 8, lineheight = 1.1),
    legend.position = "bottom",
    legend.text = ggplot2::element_text(size = 9),
    plot.caption = ggplot2::element_text(
      hjust = 0,
      size = 9,
      margin = ggplot2::margin(t = 12)
    ),
    panel.grid.minor = ggplot2::element_blank(),
    plot.margin = ggplot2::margin(18, 18, 14, 14)
  ) +
  ggplot2::guides(colour = ggplot2::guide_legend(nrow = 2, byrow = TRUE))

# A focused paired view keeps the small net difference from hiding gains/losses.
pair <- Filter(
  function(x) {
    identical(x$candidate_series, "published_1.0_raw_resolution_array")
  },
  report$overall_paired_changes
)[[1]]
paired <- data.frame(
  outcome = factor(
    c(
      "New confirmed successes",
      "Lost confirmed successes",
      "Earlier first confirmation",
      "Later first confirmation"
    ),
    levels = rev(c(
      "New confirmed successes",
      "Lost confirmed successes",
      "Earlier first confirmation",
      "Later first confirmation"
    ))
  ),
  count = c(
    pair$known_success_gain_count,
    pair$known_success_loss_count,
    pair$earlier_confirmed_event_count,
    pair$later_confirmed_event_count
  ),
  kind = c("Gain", "Loss", "Earlier", "Later")
)
summary_plot <- ggplot2::ggplot(
  paired,
  ggplot2::aes(x = outcome, y = count, fill = kind)
) +
  ggplot2::geom_col(width = 0.58, show.legend = FALSE) +
  ggplot2::geom_text(
    ggplot2::aes(label = count),
    hjust = -0.3,
    colour = "#203047",
    size = 5
  ) +
  ggplot2::coord_flip() +
  ggplot2::scale_fill_manual(
    values = c(
      Gain = "#167D7F",
      Loss = "#B85C38",
      Earlier = "#234D78",
      Later = "#8A7A99"
    )
  ) +
  ggplot2::scale_y_continuous(expand = ggplot2::expansion(mult = c(0, 0.15))) +
  ggplot2::labs(
    title = "A net change can hide paired differences",
    subtitle = "Published array score versus archived score · high-fidelity observations only",
    x = NULL,
    y = "Number of campaign pairs",
    caption = paste0(
      "Success gains/losses require the other rule to have observed no success; unknown outcomes remain separate.\n",
      "Timing counts condition on success under both rules. Configuration-specific budgets differ; the same observations are reused."
    )
  ) +
  ggplot2::theme_minimal(base_size = 12) +
  ggplot2::theme(
    plot.title = ggplot2::element_text(
      size = 19,
      face = "bold",
      colour = "#203047"
    ),
    plot.subtitle = ggplot2::element_text(
      colour = "#526174",
      margin = ggplot2::margin(b = 18)
    ),
    axis.text.y = ggplot2::element_text(colour = "#203047"),
    panel.grid.major.y = ggplot2::element_blank(),
    panel.grid.minor = ggplot2::element_blank(),
    plot.caption = ggplot2::element_text(
      hjust = 0,
      colour = "#526174",
      size = 9,
      margin = ggplot2::margin(t = 18)
    ),
    plot.margin = ggplot2::margin(22, 28, 18, 18)
  )
for (extension in c("png", "svg")) {
  device <- if (extension == "svg") grDevices::svg else "png"
  ggplot2::ggsave(
    file.path(root, paste0("trajectory-sensitivity-atlas.", extension)),
    atlas,
    width = 14,
    height = 16,
    dpi = 170,
    device = device
  )
  ggplot2::ggsave(
    file.path(root, paste0("trajectory-sensitivity-summary.", extension)),
    summary_plot,
    width = 11,
    height = 6.3,
    dpi = 170,
    device = device
  )
}
