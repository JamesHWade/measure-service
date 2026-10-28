# Plot evidence emitted by python -m measure_service.benchmarks replay-moses.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) {
  stop("Usage: Rscript scripts/plot_moses_replay.R REPLAY_DIRECTORY")
}
root <- normalizePath(args[[1]], mustWork = TRUE)
report <- jsonlite::fromJSON(
  file.path(root, "report.json"),
  simplifyVector = FALSE
)
trajectory <- utils::read.csv(file.path(root, "success-trajectories.csv"))

label_for <- function(item) {
  config <- item$config
  if (
    item$record_kind != "simulated-benchmark" ||
      config$num_segments != 3 ||
      isTRUE(config$segment_based) ||
      config$surrogate_model != "GP" ||
      config$acquisition_function != "LogEI"
  ) {
    return(NA_character_)
  }
  if (config$seed_budget == 100) {
    return("Random design (100 initial runs)")
  }
  if (config$seed_budget != 3 || config$seed_strategy != "random") {
    return(NA_character_)
  }
  task <- if (config$objective_type == "compoundwise") {
    "Per analyte"
  } else {
    "Aggregate"
  }
  fidelity <- if (isTRUE(config$multi_fidelity)) {
    "multi-fidelity"
  } else {
    "single fidelity"
  }
  flag <- if (isTRUE(config$advanced_peak_detection)) {
    "advanced flag"
  } else {
    "basic flag"
  }
  paste(task, fidelity, flag, sep = " / ")
}
labels <- vapply(report$configurations, label_for, character(1))
names(labels) <- vapply(
  report$configurations,
  function(item) item$configuration_id,
  character(1)
)
trajectory$configuration <- unname(labels[trajectory$configuration_id])
data <- trajectory[!is.na(trajectory$configuration), ]
if (nrow(data) == 0L) {
  stop("No configurations match the declared comparison profile.")
}
data$configuration <- factor(
  data$configuration,
  levels = sort(unique(data$configuration))
)

plot <- ggplot2::ggplot(
  data,
  ggplot2::aes(
    high_fidelity_budget,
    success_fraction,
    colour = configuration,
    fill = configuration
  )
) +
  ggplot2::geom_ribbon(
    ggplot2::aes(
      ymin = success_fraction,
      ymax = success_fraction_upper_bound
    ),
    alpha = 0.1,
    colour = NA
  ) +
  ggplot2::geom_step(linewidth = 0.75) +
  ggplot2::scale_y_continuous(
    limits = c(0, 1),
    labels = function(x) paste0(round(100 * x), "%")
  ) +
  ggplot2::labs(
    title = "MoSeS archived benchmark replay",
    subtitle = "Recorded BoTier score >= 2; complete 50-mixture denominator",
    x = "High-fidelity evaluations, including initial design",
    y = "Mixtures with recorded satisfactory score",
    colour = NULL,
    fill = NULL,
    caption = paste(
      "Source: Zenodo 10.5281/zenodo.22109729. Reconciled archived histories; no new optimization.",
      "Shading bounds outcomes unknown from the archive; it is not a confidence interval.",
      "All observations here use simulator latent values. The advanced flag is not a processing validation.",
      sep = "\n"
    )
  ) +
  ggplot2::theme_minimal(base_size = 11) +
  ggplot2::theme(
    legend.position = "bottom",
    legend.text = ggplot2::element_text(size = 8),
    plot.caption = ggplot2::element_text(hjust = 0, size = 8),
    panel.grid.minor = ggplot2::element_blank()
  ) +
  ggplot2::guides(
    colour = ggplot2::guide_legend(ncol = 2),
    fill = ggplot2::guide_legend(ncol = 2)
  )
ggplot2::ggsave(
  file.path(root, "success-trajectories.png"),
  plot,
  width = 10,
  height = 7,
  dpi = 180
)
ggplot2::ggsave(
  file.path(root, "success-trajectories.svg"),
  plot,
  width = 10,
  height = 7,
  device = grDevices::svg
)
